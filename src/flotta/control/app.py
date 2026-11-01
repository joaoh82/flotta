"""The control plane — fleet state over HTTP (M4.5).

§8.6 gives this one line: "control plane as a deployable service; Postgres;
Dockerfile." M4 landed the Postgres half. This is the service: a small
always-on process that owns the reconcile loop and serves the fleet to
whatever wants to read it.

It is §8.1's "boring block" — ~256MB and a public HTTPS endpoint, deployable on
anything. The interesting requirements all live below the `Backend` line.

## Authentication (M5)

Every `/api/*` route requires a scoped token signed with `$FLOTTA_SIGNING_KEY`
— see `flotta.auth` for the token design and why revocation is a key rotation.
Routes declare the scope they need; `box:destroy` is separate from
`fleet:write` because `DELETE /api/boxes/{id}` destroys someone's agent *and
its memory*, and a dashboard that shows a fleet should not carry that.

`/health` is deliberately open. It reports whether the reconcile loop is
sweeping and nothing about the fleet's contents, and a liveness probe cannot
hold a credential.

The bind guard survives, with a smaller job. It used to mean "there is no way
to authenticate this, so do not expose it"; it now means "auth is not
configured, so do not expose it". Copying Hermes's own gate was deliberate: M3
hit that gate, tried to work around it with a socat forwarder, and the
workaround was the wrong answer — the gate was right.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import ipaddress
import logging
import os
import threading
import time
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any

from flotta import db
from flotta.address import parse as parse_address
from flotta.auth import (
    CHAT_PROTOCOL,
    DEFAULT_CLIENT,
    DEFAULT_ORG,
    SCOPE_AUTH_RELAY,
    SCOPE_BOX_CHAT,
    SCOPE_BOX_DESTROY,
    SCOPE_BOX_PEER,
    SCOPE_FLEET_READ,
    SCOPE_FLEET_ROUTE,
    SCOPE_FLEET_WRITE,
    SCOPE_GIT_CREDENTIAL,
    SCOPE_ORG_ADMIN,
    SIGNING_KEY_ENV,
    TICKET_PROTOCOL_PREFIX,
    TICKET_TTL_S,
    USER_SUBJECT_PREFIX,
    AuthError,
    Token,
    admits,
    admits_tenant,
    mint,
    mint_ticket,
    person_token,
    resolve_signing_key,
    scopes_for_role,
    subject_box,
    verify,
)
from flotta.control.loop import DEFAULT_INTERVAL_S, LoopState, run_reconcile_loop
from flotta.control.ratelimit import RateLimit, hit_all
from flotta.relay import (
    DEFAULT_TIMEOUT_S as RELAY_TIMEOUT_S,
)
from flotta.relay import (
    EVENT_TEXT_CHARS,
    PEER_ANSWERED,
    PEER_ASKED,
    PEER_ASKED_BY,
    PEER_FAILED,
    PEER_REFUSED,
    PEER_REPLIED,
    Chains,
    RelayRefused,
    deliver_through_door,
    envelope,
)
from flotta.store import (
    AGENCY_GITHUB_UNAVAILABLE,
    INVITE_TTL_S,
    LINK_TTL_S,
    DuplicateMembershipError,
    DuplicateTenantError,
    FleetStore,
    InvalidAccountError,
    InvalidTenantError,
    InvalidTransitionError,
    LastOwnerError,
    NotOperatorError,
    OrgNotActiveError,
    UnknownEntityError,
    is_terminal,
    normalize_email,
    reaches_fleet_github,
)

try:
    # At module level because FastAPI resolves a route's annotations against
    # this module's globals, and these two are only ever annotations. The rest
    # of FastAPI is imported in `create_app`, so the CLI can import this module
    # without the `control` extra.
    from fastapi import BackgroundTasks, Request
except ImportError:  # pragma: no cover - create_app's own import says so
    BackgroundTasks = Request = Any  # type: ignore[assignment,misc]

_log = logging.getLogger("flotta.control")

#: What a read-only session may still be used for (M12.9). Looking, and
#: ending itself: signing out changes nothing about the org being viewed.
_LOOKING = frozenset({"GET", "HEAD", "OPTIONS"})
_ENDS_ITSELF = ("POST", "/api/auth/sign-out")

#: A read-only session's token: what an owner may read, and no scope that
#: writes. The method check in `_session_token` is what refuses a change;
#: this is so a route that forgot to be a GET still meets a 403.
VIEW_AS_SCOPES = frozenset({SCOPE_FLEET_READ, SCOPE_ORG_ADMIN})

#: Live provisioning threads, held so nothing collects them mid-flight.
_provisioning: set[threading.Thread] = set()

#: Agents whose model is being changed by a thread of this process (FLOTTA-99).
#: The timeline says the same thing, but two requests can both read it before
#: either writes; this is the check that holds between them.
_model_changes_running: set[str] = set()
_model_changes_lock = threading.Lock()


def _build() -> str:
    """Which commit is running.

    `/health` said the process was up and nothing about *what* it was. That is
    indistinguishable from the interesting question during a deploy: a restart
    caused by a changed variable looks exactly like a restart caused by new
    code, and reading one as the other cost this project a wasted machine and
    an hour of misdiagnosis — the bug was hunted in Railway's variables, which
    were correct all along.

    Render sets `RENDER_GIT_COMMIT` and Railway `RAILWAY_GIT_COMMIT_SHA`;
    `$FLOTTA_BUILD` is the override for anywhere else. Unknown is reported as
    unknown rather than guessed.
    """
    for name in ("FLOTTA_BUILD", "RENDER_GIT_COMMIT", "RAILWAY_GIT_COMMIT_SHA"):
        value = (os.environ.get(name) or "").strip()
        if value:
            return value[:12]
    return "unknown"


def _peek_for(impl: Any, name: str) -> str | None:
    """The endpoint `create` would adopt for a name, or None.

    Best-effort by construction: a probe that fails must not stop a create, or
    a Fly hiccup becomes an outage for the one verb that adds capacity.
    """
    from flotta.provision import _peek_endpoint

    try:
        return _peek_endpoint(impl, name)
    except Exception:  # noqa: BLE001
        return None


LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})

#: Sign-in links one address may ask for per link lifetime. Enough for a
#: person who mistyped, deleted the email or switched devices.
LINK_LIMIT_PER_ADDRESS = 5
#: And one caller, across addresses. The web app's server is one caller for
#: every browser, so it names the browser (`CALLER_IP_HEADER`) and the limit
#: applies to that; anyone else is limited by the address they connect from.
LINK_LIMIT_PER_IP = 30
LINK_LIMIT_WINDOW_S = LINK_TTL_S
#: Invites one org may send per window. A pending org is a stranger who signed
#: up, and an invite carries the org's own name to any address it is given, so
#: a pending org sends enough to bring in a small team and no more.
INVITE_LIMIT_PER_ORG = 20
INVITE_LIMIT_PER_PENDING_ORG = 3
#: The browser behind a sign-in request, as the web app's server says. Only
#: believed on the operator's token holding `auth:relay`: from anyone else it
#: would be a way to pick a fresh bucket for every request.
CALLER_IP_HEADER = "X-Flotta-Caller-IP"
#: How many proxies in front of the control plane append to `X-Forwarded-For`.
#: Unset is none, and the caller is whoever connected: on Render, that is
#: Render's proxy, for everybody.
TRUSTED_PROXY_HOPS_ENV = "FLOTTA_TRUSTED_PROXY_HOPS"


def resolve_trusted_proxy_hops(env: dict[str, str] | None = None) -> int:
    env = os.environ if env is None else env
    raw = (env.get(TRUSTED_PROXY_HOPS_ENV) or "").strip()
    if not raw:
        return 0
    if not raw.isdigit():
        raise ValueError(f"${TRUSTED_PROXY_HOPS_ENV} is a count of proxies, not {raw!r}")
    return int(raw)


def caller_address(peer: str | None, forwarded_for: list[str], hops: int) -> str:
    """Where a request came from, seen past `hops` proxies that are trusted.

    Each proxy appends the address it was reached from, so the entry `hops`
    from the right is what the outermost trusted proxy saw. Everything left of
    it the caller wrote, and could have made up: reading the leftmost entry
    would hand every request a fresh bucket for the price of a header. Fewer
    entries than hops means the request did not come through those proxies,
    and the peer is all there is.
    """
    entries = [e.strip() for line in forwarded_for for e in line.split(",") if e.strip()]
    if hops == 0 or len(entries) < hops:
        return peer or "unknown"
    return entries[-hops]


INTERVAL_ENV = "FLOTTA_RECONCILE_INTERVAL_S"
#: The credential boxes borrow. Held here, never on a box — see
#: `git_credential` for what that does and does not buy.
GITHUB_TOKEN_ENV = "FLOTTA_GITHUB_TOKEN"


def _default_reachable(repo: str, *, token: str | None) -> Any:
    """The real GitHub check, behind a function so `create_app` can replace it.

    Imported lazily: `flotta.github` pulls in `httpx` only when it actually
    asks, and the CLI imports this module for `flotta serve`.
    """
    from flotta.github import repo_reachable

    return repo_reachable(repo, token=token)


class InsecureBindError(RuntimeError):
    """Refused to expose the fleet without authentication."""


def check_bind(host: str, *, env: dict[str, str] | None = None) -> None:
    """Refuse a non-loopback bind while there is no auth to put in front of it.

    Three states, and each is defensible on its own:

    - **no key, loopback** — runs unauthenticated. This is local development,
      where the port is already only reachable by whoever is sitting there, and
      demanding a token to run `just serve` would buy nothing.
    - **no key, public** — refused. Fails closed at startup rather than
      warning: a warning in a deploy log is read once, by the person who
      already knew, and the thing being protected is a kill switch for an
      agent's whole memory.
    - **key set, any bind** — every request needs a scoped token.

    `FLOTTA_CONTROL_ALLOW_INSECURE_BIND` is **gone**. It existed because there
    was no way to authenticate a public bind, so the only options were "refuse"
    or "refuse unless you promise you own the network". There is a way now, and
    keeping an override that skips it would mean shipping the hole this
    milestone closed.
    """
    env = os.environ if env is None else env
    if host in LOOPBACK_HOSTS:
        return
    if resolve_signing_key(env):
        return
    raise InsecureBindError(
        f"refusing to bind {host!r} with no authentication: "
        f"DELETE /api/boxes/<id> destroys a box and everything it remembers.\n"
        f"Configure a signing key — `flotta token key` and set ${SIGNING_KEY_ENV} "
        f"— or bind 127.0.0.1 and reach it over a tunnel."
    )


def _store_factory() -> FleetStore:
    """A fresh store per request.

    Per-request rather than one long-lived connection, for the same reason the
    dashboard opens SQLite per request: a pinned snapshot served to a polling
    UI is stale in the one way a fleet view must never be. Connecting is cheap
    on both engines.
    """
    # No argument on purpose: FleetStore owns the $FLOTTA_DATABASE_URL /
    # $FLOTTA_STORE / ./fleet.db chain. This function used to re-implement it
    # and got it wrong — the service honoured the database URL but not
    # $FLOTTA_STORE, and served an empty fleet from a file nobody meant while
    # `flotta ps` in the same shell listed the real one.
    return FleetStore()


#: Do not write an `addressed` event more often than this. Without a floor,
#: every HTTP request through the door would append a row — the event log is
#: the fleet's history, not a request log, and one row per request would bury
#: the transitions a human actually reads it for.
ACTIVITY_FLOOR_S = 60.0


def _record_activity(store: Any, box_id: str) -> bool:
    """Note that a box was addressed, at most once a minute.

    This is the activity signal idle-sleep runs on. It lives in the event log
    rather than a `boxes.last_active_at` column because the store has no
    migration machinery — the schema is `CREATE TABLE IF NOT EXISTS` and
    nothing else, so a new column would silently break every existing fleet.

    Rate-limited by reading the newest event first, which costs one query on a
    path that is already making an HTTP round trip.
    """
    from datetime import UTC, datetime

    from flotta.provision import ADDRESSED_EVENT, last_activity_at

    try:
        seen = last_activity_at(store, box_id)
    except Exception:  # pragma: no cover - a box that vanished mid-request
        return False
    if seen is not None and (datetime.now(UTC) - seen).total_seconds() < ACTIVITY_FLOOR_S:
        return False
    store.add_event("box", box_id, ADDRESSED_EVENT, {"via": "front-door"})
    return True


def _build_region() -> str:
    """Where the throwaway build machine is created.

    `fly volumes create` refuses to run without a region off a TTY and a deploy
    wants one too, so "unset" cannot mean "decide later" — `resolved_region`
    turns an unset config into a concrete one, exactly as `fly-up` does.
    """
    from flotta.fly import FlyConfig

    return FlyConfig.from_env().resolved_region()


def _box_dict(
    box: Any,
    meta: Any = None,
    *,
    model: str | None = None,
    fleet_model: str | None = None,
    given: str | None = None,
    model_change: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """The row, plus who the agent is when the caller has looked that up.

    `display_name` / `description` / `instructions` are always present so the
    app never reads `undefined` — `None` means "not set", which is a real
    state. A read route then removes `instructions` for a caller who may not
    see them (`_for_reader`), so absent means "not yours", never "none".

    `meta` is optional because the identity is a side table: the list
    endpoint fetches it in one query for the fleet, the single-box endpoints
    fetch one row, and a few internal callers have no store handy and get
    Nones — which is honest, since they did not look.
    """
    from dataclasses import asdict

    out = asdict(box)
    out["display_name"] = meta.display_name if meta else None
    out["description"] = meta.description if meta else None
    out["instructions"] = meta.instructions if meta else None
    # What model the agent runs (FLOTTA-39), and whose choice that is: its
    # own, or the fleet's. `None` for both means the fleet has no model at
    # all, which the window should say rather than guess.
    #
    # For one that follows the fleet, what its machine was `given` wins over
    # the default (FLOTTA-86): a new default reaches no existing agent, and
    # reporting it as what they run made the window wrong the moment the
    # operator changed it. `fleet_model` is the default now, for comparing.
    out["model"] = model or given or fleet_model
    out["model_source"] = "agent" if model else ("fleet" if out["model"] else None)
    out["fleet_model"] = fleet_model
    # A change under way, or the last one failing (FLOTTA-99): what lets the
    # window say "restarting" and an open conversation expect the drop.
    out["model_change"] = model_change
    return out


def _with_meta(store: Any, box: Any) -> dict[str, Any]:
    from flotta.provision import fleet_model, model_changes, model_given_by_default

    return _box_dict(
        box,
        store.meta_for_box(box.id),
        model=store.model_for_box(box.id),
        fleet_model=fleet_model(store),
        given=store.models_given([box.id]).get(box.id) or model_given_by_default(),
        model_change=model_changes(store, [box.id]).get(box.id),
    )


def _reads_instructions(token: Token | None) -> bool:
    """Whether `token` may read an agent's standing instructions (FLOTTA-84).

    Whoever may change them (`fleet:write`), and the operator viewing an agency
    as its owner (`org:admin`, read-only). Not a client viewer: instructions
    are how an agency runs the agent *for* that client, and a client reads its
    description instead. `None` is a loopback control plane with no auth,
    whose caller is the operator.
    """
    return token is None or bool(token.scopes & {SCOPE_FLEET_WRITE, SCOPE_ORG_ADMIN})


def _for_reader(row: dict[str, Any], token: Token | None) -> dict[str, Any]:
    """`row`, without the instructions if `token` may not read them."""
    if not _reads_instructions(token):
        row.pop("instructions", None)
    return row


#: Where a stored diagnostic may carry an agent's instructions: Fly's error
#: quoted a failed `machine run` whole, `--env FLOTTA_INSTRUCTIONS=…` included,
#: and an `exec` as its `/bin/sh -c` command, which for an instruction write is
#: those instructions in base64. `fly_backend._shown` stopped both at the
#: source; events recorded before that are still in the store.
_INSTRUCTION_MARKERS = ("FLOTTA_INSTRUCTIONS=", "/bin/sh -c ")
WITHHELD = "… (withheld: it may contain the agent's standing instructions)"


def _withheld(value: Any, instructions: str | None) -> Any:
    """`value` with the agent's instructions taken out, for a caller who may
    not read them (FLOTTA-84): every string cut where a marker begins, and the
    current instructions replaced wherever they appear whole.

    Cut to the end, because an old diagnostic quoted the command unquoted and
    nothing marks where the instructions in it stop. A marker already followed
    by `…` was redacted where it was written, and the rest of that string,
    Fly's own reason among it, is kept."""
    if isinstance(value, str):
        for marker in _INSTRUCTION_MARKERS:
            at = 0
            while (at := value.find(marker, at)) >= 0:
                at += len(marker)
                if not value.startswith("…", at):
                    value = value[:at] + WITHHELD
                    break
        if instructions and instructions in value:
            value = value.replace(instructions, WITHHELD)
        return value
    if isinstance(value, dict):
        return {k: _withheld(v, instructions) for k, v in value.items()}
    if isinstance(value, list):
        return [_withheld(v, instructions) for v in value]
    return value


def _epoch(iso: str | None) -> float | None:
    """A store timestamp as seconds since the Unix epoch, or None.

    **Epoch rather than the ISO string the store keeps**, because the only
    consumer compares it against a Hermes session's `started_at`, which is a
    float. Handing the app two formats to reconcile would put a date parser in
    the window for a comparison that is a subtraction.

    A timestamp that will not parse returns None rather than raising: this
    decides whether to reuse a conversation, and a malformed row must not take
    down the read of an agent that is otherwise fine.
    """
    if not iso:
        return None
    try:
        return datetime.fromisoformat(iso).timestamp()
    except ValueError:
        return None


def create_app(
    *,
    store_factory: Any = _store_factory,
    run_loop: bool = True,
    #: Answer `202` and provision on a background task rather than holding the
    #: request open for minutes. Off in tests, where a synchronous create is
    #: what the assertions are about — and where there is no proxy to time out.
    background: bool = True,
    interval_s: float | None = None,
    loop_runner: Any = None,
    signing_key: str | None = None,
    #: Asks GitHub whether the fleet token can reach a repository, before a
    #: grant is stored. Injected for the same reason every Fly touchpoint is:
    #: the suite must stay hermetic and $0, and the default here would make a
    #: real HTTPS call the moment `$FLOTTA_GITHUB_TOKEN` was set in the
    #: environment a test happened to inherit.
    reachable: Any = None,
    #: Carries one message to another agent and returns its reply. Injected
    #: for the same reason as `reachable`, and with more at stake: the default
    #: opens a WebSocket to a live box through the front door and spends a
    #: model call. An `async (peer_name, text, *, title) -> str`.
    deliver: Any = None,
    #: Whether a model id exists before an agent is pointed at it. Injected
    #: for the reason `reachable` is: the default asks OpenRouter over HTTPS.
    #: A `(model, base_url) -> model_catalog.Known`.
    known_model: Any = None,
    #: Delivers a sign-in link: a `mail.Mailer`. Injected so the suite sends
    #: nothing; `None` resolves it from the environment (`mail.resolve_mailer`),
    #: which may also decide there is no way to deliver one.
    mailer: Any = None,
    #: Where sign-in links point: the web app. `None` reads `$FLOTTA_APP_URL`.
    app_url: str | None = None,
    #: Proxies in front of this process (`caller_address`). `None` reads
    #: `$FLOTTA_TRUSTED_PROXY_HOPS`.
    trusted_proxy_hops: int | None = None,
) -> Any:
    """Build the control-plane app.

    `run_loop=False` is for tests and for anyone running a second replica that
    should serve reads without racing the first one's reconciliation — two
    loops reconciling the same fleet is not harmful (the store's transitions
    are validated) but it is wasted backend calls.

    `signing_key` overrides `$FLOTTA_SIGNING_KEY`. **Resolved once, here, at
    construction** rather than per request: an app that picks up a key change
    mid-flight would silently start accepting tokens it was rejecting a moment
    earlier, and "is this thing authenticated?" would have no stable answer.
    Rotating the key means restarting the process, which is also what makes
    rotation a real revocation.
    """
    from fastapi import Depends, FastAPI, Header, HTTPException

    from flotta.mail import (
        invite_message,
        resolve_app_url,
        resolve_mailer,
        sign_in_message,
        sign_in_url,
        sign_up_message,
    )

    resolved_interval = interval_s
    if resolved_interval is None:
        raw = (os.environ.get(INTERVAL_ENV) or "").strip()
        resolved_interval = float(raw) if raw else DEFAULT_INTERVAL_S

    state = LoopState(interval_s=resolved_interval)

    # Where new agents go, checked now: a typo in `$FLOTTA_BACKEND` refuses to
    # start, rather than starting healthy and failing the first create.
    from flotta.backend import default_scheme

    default_scheme()

    key = signing_key if signing_key is not None else resolve_signing_key()
    if key is None:
        _log.warning(
            "no %s configured: the fleet API is UNAUTHENTICATED. Anyone who can "
            "reach this port can destroy any box and everything it remembers. "
            "This is refused on a non-loopback bind; mint a key with "
            "`flotta token key` before exposing it.",
            SIGNING_KEY_ENV,
        )

    deliver_link = mailer if mailer is not None else resolve_mailer(keyless=not key)
    if deliver_link is None:
        _log.warning(
            "no $FLOTTA_RESEND_API_KEY: sign-in links cannot be delivered, so none "
            "will be made. Set it, or $FLOTTA_PRINT_SIGN_IN_LINKS=1 to print them "
            "to this log on a machine whose log only you can read."
        )
    link_base = app_url if app_url is not None else resolve_app_url()
    # Where tickets send a browser: refused now, not in a ticket nobody can use.
    from flotta.client import browser_door

    browser_door()
    per_address = RateLimit(LINK_LIMIT_PER_ADDRESS, LINK_LIMIT_WINDOW_S)
    per_ip = RateLimit(LINK_LIMIT_PER_IP, LINK_LIMIT_WINDOW_S)
    invites_per_org = RateLimit(INVITE_LIMIT_PER_ORG, LINK_LIMIT_WINDOW_S)
    invites_per_pending_org = RateLimit(INVITE_LIMIT_PER_PENDING_ORG, LINK_LIMIT_WINDOW_S)
    proxy_hops = (
        trusted_proxy_hops if trusted_proxy_hops is not None else resolve_trusted_proxy_hops()
    )

    def require(*scopes: str, any_of: bool = False):
        """A dependency that admits a request carrying **all** of `scopes`.

        All rather than any: a route that needs two permissions needs two, and
        an "any" default is the kind of thing that reads as fine until someone
        adds a second scope to a route and quietly widens it. `any_of=True` is
        the explicit exception, for the two routes the door shares with a
        person (`needs_route`).

        With no key configured this admits everything, which is the documented
        loopback-development state — `check_bind` is what stops that reaching a
        public interface, and it is enforced at startup rather than here so the
        failure is a refusal to boot rather than a 401 nobody sees.

        **A session is honoured with or without a key** (M12.2): it is a row in
        the store, not a signature, and a web app run locally against a keyless
        control plane must see one agency's view, not the operator's over
        everything. It becomes the token its membership would mint
        (`_session_token`), and from here on nothing can tell the two apart.
        """

        def dependency(
            request: Request, authorization: str | None = Header(default=None)
        ) -> Token | None:
            scheme, _, credential = (authorization or "").partition(" ")
            if scheme.lower() == "session":
                token = _session_token(credential.strip(), request)
            elif key is None:
                return None
            elif scheme.lower() != "bearer":
                # 401 with the challenge, not 403: the caller sent nothing, so
                # the answer is "authenticate", not "you may not".
                raise HTTPException(
                    status_code=401,
                    detail="missing bearer token",
                    headers={"WWW-Authenticate": "Bearer"},
                )
            else:
                try:
                    token = verify(credential.strip(), key=key)
                except AuthError as exc:
                    raise HTTPException(
                        status_code=401,
                        detail=str(exc),
                        headers={"WWW-Authenticate": "Bearer"},
                    ) from exc

            missing = [s for s in scopes if not token.allows(s)]
            if any_of and len(missing) < len(scopes):
                missing = []
            if missing:
                # 403, not 401: the token is valid and re-authenticating with
                # it will not help. Name the scope so the fix is obvious.
                raise HTTPException(
                    status_code=403,
                    detail=f"token for {token.subject!r} lacks scope(s): {', '.join(missing)}",
                )
            return token

        return dependency

    def _session_token(secret: str, request: Request) -> Token:
        """The token a live session's membership would mint, or 401.

        Asked on every request rather than fixed at sign-in, so ending the
        session, disabling the account or removing the membership takes effect
        on the next request, not when a month-long session would have expired.

        **A read-only session changes nothing** (M12.9, the operator's "view
        as owner"). Every request made with one that is not a read is refused
        here, before any route runs, whatever the route and whatever its
        scope; `control/test_view_as.py` sends one to every route there is.
        It is honoured only while its holder is still the operator, since it
        is no membership of the org it looks at.
        """
        refused = HTTPException(
            status_code=401,
            detail="no such session, or it has ended; sign in again",
            headers={"WWW-Authenticate": "Session"},
        )
        store = store_factory()
        try:
            session = store.session(secret)
            if session is None:
                raise refused
            user = store.get_user(session.user_id)
            if session.read_only:
                looking = store.is_operator(session.user_id) and store.get_org(session.org)
                membership = None
            else:
                membership = store.membership(session.user_id, session.org)
        finally:
            store.close()
        if user is None or user.disabled_at is not None:
            raise refused
        if session.read_only:
            if not looking:
                raise refused
            if request.method not in _LOOKING and (request.method, request.url.path) != (
                _ENDS_ITSELF
            ):
                raise HTTPException(
                    status_code=403,
                    detail=f"you are viewing {session.org!r} as its owner, which is read-only",
                )
            owner = person_token(
                user_id=user.id,
                org=session.org,
                role="owner",
                client=None,
                issued_at=int(datetime.fromisoformat(session.created_at).timestamp()),
                expires_at=int(datetime.fromisoformat(session.expires_at).timestamp()),
            )
            return dataclasses.replace(owner, scopes=VIEW_AS_SCOPES)
        if membership is None:
            raise refused
        try:
            return person_token(
                user_id=user.id,
                org=session.org,
                role=membership.role,
                client=membership.client,
                issued_at=int(datetime.fromisoformat(session.created_at).timestamp()),
                expires_at=int(datetime.fromisoformat(session.expires_at).timestamp()),
            )
        except AuthError as exc:  # a role this build no longer knows
            raise refused from exc

    def _session_of(authorization: str | None) -> Any:
        """The session a request was made with, or 403 if it was not a session.

        For the routes about one's own sessions. A token is not signed out and
        has no sessions to list: it ends when it expires or the key rotates.
        """
        scheme, _, credential = (authorization or "").partition(" ")
        store = store_factory()
        try:
            session = store.session(credential.strip()) if scheme.lower() == "session" else None
        finally:
            store.close()
        if session is None:
            raise HTTPException(
                status_code=403,
                detail="only a signed-in person has sessions; this request carries a token",
            )
        return session

    # Hoisted rather than called inline in each route's default: a call in an
    # argument default is evaluated once at import, which is fine for Depends
    # but is a real footgun in general — so the linter flags it, and naming the
    # three dependencies is clearer than suppressing it five times.
    needs_read = Depends(require(SCOPE_FLEET_READ))
    needs_write = Depends(require(SCOPE_FLEET_WRITE))
    needs_destroy = Depends(require(SCOPE_BOX_DESTROY))
    needs_git = Depends(require(SCOPE_GIT_CREDENTIAL))
    needs_peer = Depends(require(SCOPE_BOX_PEER))
    needs_admin = Depends(require(SCOPE_ORG_ADMIN))
    needs_chat = Depends(require(SCOPE_BOX_CHAT))
    # Any valid credential at all: for the routes about the caller themselves.
    needs_anyone = Depends(require())
    # The door's `fleet:route`, or a person's `box:chat` — see `_routable_box`.
    needs_route = Depends(require(SCOPE_FLEET_ROUTE, SCOPE_BOX_CHAT, any_of=True))
    # Called, not a dependency: only a request naming its caller needs it.
    check_relay = require(SCOPE_AUTH_RELAY)

    # -- whose box: the tenant scope (M11.6a) ------------------------------
    #
    # Every route that names a box finds it through `_scoped_box`, and the
    # list through `_scoped_boxes`; `control/test_route_scope.py` reads this
    # file and fails a route that does not. A token reaches what `admits_tenant`
    # allows — its own org, or its own client — and **no token at all means no
    # signing key**, the loopback development path, where the caller is the
    # operator over the whole store, exactly as the CLI is.

    def _is_operator(token: Token | None) -> bool:
        """The default org's, unconfined: the person who runs the fleet.

        Not a box: a box's token is the default org's too, and lives on a
        machine whose agent has root.
        """
        if token is None:
            return True
        return (
            (token.org or DEFAULT_ORG) == DEFAULT_ORG
            and token.client is None
            and subject_box(token.subject) is None
        )

    def _require_operator(token: Token | None, action: str) -> None:
        """403 unless the operator: for the routes that act on the whole fleet.

        403, not the 404 a box answers with: the route plainly exists for
        everyone, and what is refused is the act, not knowledge of a box.
        """
        if not _is_operator(token):
            raise HTTPException(
                status_code=403, detail=f"only the fleet's operator (the default org) may {action}"
            )

    def _scoped_box(store: Any, token: Token | None, ref: str) -> Any:
        """A box by id or name that `token` may act on, or 404.

        Another org's box is the same 404 as no box: "exists, but not yours"
        would make every route a directory of other people's agents.
        """
        box = store.get_box(ref) or store.get_box_by_name(ref)
        if box is None or (
            token is not None and admits_tenant(token, org=box.org, client=box.client) is not None
        ):
            raise HTTPException(status_code=404, detail=f"no box {ref!r}")
        return box

    def _operated_box(store: Any, token: Token | None, ref: str) -> Any:
        """`_scoped_box`, except that the operator reaches any org's box (FLOTTA-86).

        For the console's own acts on one agent: its machine, its timeline, an
        upgrade and its repository grants, done with the operator's session.
        View-as is no way round a 404 there, because it is read-only by design.
        Anyone else gets exactly `_scoped_box`, the same 404 for another org's
        box, and a box's own token is never the operator (`_is_operator`).
        """
        if token is not None and _is_operator(token):
            box = store.get_box(ref) or store.get_box_by_name(ref)
            if box is None:
                raise HTTPException(status_code=404, detail=f"no box {ref!r}")
            return box
        return _scoped_box(store, token, ref)

    def _scoped_boxes(store: Any, token: Token | None) -> list[Any]:
        """The boxes `token` may see: its org's, or its client's."""
        if token is None:
            return store.list_boxes()
        return store.list_boxes_in(token.org or DEFAULT_ORG, token.client)

    def _routable_box(store: Any, token: Token | None, ref: str) -> Any:
        """`_scoped_box`, except for the front door, which finds any org's box.

        The door resolves a hostname *before* it knows whose box it names, and
        then checks the caller's token against the answer itself. So its own
        token holds `fleet:route` and is the operator's; a tenant's token
        carrying the scope gains nothing, which is the point of requiring both.
        """
        if token is not None and token.allows(SCOPE_FLEET_ROUTE) and _is_operator(token):
            box = store.get_box(ref) or store.get_box_by_name(ref)
            if box is None:
                raise HTTPException(status_code=404, detail=f"no box {ref!r}")
            return box
        return _scoped_box(store, token, ref)

    def _occupant(store: Any, token: Token | None, endpoint: str) -> str | None:
        """Why a machine is taken, as far as `token` may be told; `None` if free.

        Fleet-wide on purpose: occupancy is a fact about the substrate, and a
        machine held by another org is still held. Only the *name* is scoped.
        Unreachable across orgs in practice — a box's Fly app is named after
        its whole address — but this is the place a misconfiguration would show.
        """
        for existing in store.list_boxes():
            if existing.endpoint == endpoint and not is_terminal("box", existing.status):
                if (
                    token is None
                    or admits_tenant(token, org=existing.org, client=existing.client) is None
                ):
                    return f"box {existing.id} ({existing.name}) already occupies {endpoint}"
                return f"another box already occupies {endpoint}"
        return None

    #: Delegations in flight, so a chain can be recognised as one. Per app
    #: rather than per module: two tests building two apps must not share a
    #: hop counter, and neither must two fleets in one process.
    chains = Chains()

    @asynccontextmanager
    async def lifespan(app: Any):
        task: asyncio.Task | None = None
        if run_loop:
            # `loop_runner` makes the *interesting* failure testable: a loop
            # that is configured and supposed to be running, but is not
            # sweeping. That is what a slept platform looks like from inside
            # the process, and it is the case `/health` exists to catch — a
            # real loop cannot test it, because a real loop sweeps.
            runner = loop_runner or run_reconcile_loop
            task = asyncio.create_task(runner(state, store_factory=store_factory))
        try:
            yield
        finally:
            if task is not None:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task

    app = FastAPI(title="Flotta control plane", lifespan=lifespan)
    app.state.loop_state = state

    @app.get("/health")
    def health() -> Any:
        """Liveness, and whether the reconcile loop is actually sweeping.

        Returns 503 when the loop has gone stale. "The process is up" is not
        the interesting question — a slept loop looks exactly like a healthy
        one from the outside, right up until a task strands. This is the
        difference between finding that out from a health check and finding it
        out at 138 hours.
        """
        from fastapi.responses import JSONResponse

        loop = state.snapshot()
        # Stale *or* failing. A loop that runs on schedule and errors every
        # time keeps `last_sweep_at` fresh, so staleness alone would call it
        # healthy — which it did, on the first live run, while every sweep was
        # failing on a threading bug.
        healthy = not (loop["stale"] or loop["failing"]) if run_loop else True
        return JSONResponse(
            {
                "status": "ok" if healthy else "degraded",
                "build": _build(),
                "reconcile_loop": loop,
            },
            status_code=200 if healthy else 503,
        )

    # -- orgs and clients (M11.7) ------------------------------------------
    #
    # The operator creates an org and hands its owner one token; the owner adds
    # the org's clients and mints its people's tokens from then on. Another
    # org is the 404 of no org, as another org's box is the 404 of no box.

    def _admin_of(token: Token | None, org_id: str) -> None:
        """404 unless `token` may administer `org_id`: the operator, or the org's own.

        The scope (`org:admin`) is checked by the route's dependency; this is
        the *which org* half. A client-confined token cannot hold the scope —
        `mint` refuses it — so the org's own is always org-wide here.
        """
        if _is_operator(token):
            return
        assert token is not None
        if (token.org or DEFAULT_ORG) != org_id or subject_box(token.subject) is not None:
            raise HTTPException(status_code=404, detail=f"no org {org_id!r}")

    def _org_or_404(store: Any, org_id: str) -> Any:
        org = store.get_org(org_id)
        if org is None:
            raise HTTPException(status_code=404, detail=f"no org {org_id!r}")
        return org

    @app.get("/api/orgs")
    def list_orgs(status: str | None = None, token: Token | None = needs_read) -> Any:
        """Every org, for the operator; for anyone else, their own.

        `?status=pending` is the operator's queue of sign-ups to decide.
        """
        from dataclasses import asdict

        store = store_factory()
        try:
            if _is_operator(token):
                orgs = store.list_orgs(status=status)
            else:
                assert token is not None
                own = store.get_org(token.org or DEFAULT_ORG)
                orgs = [own] if own is not None else []
            return {"orgs": [asdict(o) for o in orgs]}
        finally:
            store.close()

    @app.post("/api/orgs", status_code=201)
    def create_org(body: dict[str, Any], token: Token | None = needs_admin) -> Any:
        """Add an agency: `{"id": "agency-x", "name": "Agency X"}`. The operator's alone.

        The id is part of every address in the org and never changes, so it is
        refused here, before anything exists, if it could not be one.
        """
        _require_operator(token, "create an org")
        from dataclasses import asdict

        from flotta.store import DuplicateTenantError, InvalidTenantError

        store = store_factory()
        try:
            org = store.create_org(str(body.get("id") or ""), str(body.get("name") or ""))
        except InvalidTenantError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except DuplicateTenantError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        finally:
            store.close()
        return {"org": asdict(org)}

    @app.post("/api/orgs/{org_id}/approve")
    def approve_org(org_id: str, token: Token | None = needs_admin) -> Any:
        """Let a signed-up org create agents. The operator's alone, and final."""
        _require_operator(token, "approve an org")
        return _decide(org_id, approve=True)

    @app.post("/api/orgs/{org_id}/decline")
    def decline_org(org_id: str, token: Token | None = needs_admin) -> Any:
        """Refuse a signed-up org. It keeps its sign-in and creates nothing, ever."""
        _require_operator(token, "decline an org")
        return _decide(org_id, approve=False)

    def _decide(org_id: str, *, approve: bool) -> Any:
        from dataclasses import asdict

        store = store_factory()
        try:
            org = store.decide_org(org_id, approve=approve)
        except UnknownEntityError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except InvalidTransitionError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        finally:
            store.close()
        return {"org": asdict(org)}

    # -- the operator's view (M12.9) ------------------------------------------
    #
    # Everyone, every org's agents, and an agency seen as its owner sees it.
    # Each is the operator's alone, and says so first.

    @app.post("/api/orgs/{org_id}/view-as", status_code=201)
    def view_as(
        org_id: str,
        token: Token | None = needs_read,
        authorization: str | None = Header(default=None),
    ) -> Any:
        """A read-only session in `org_id`: the agency as its owner sees it.

        For the operator's own session: a person is looking, and the session
        says who. It lasts an hour, and nothing made with it changes anything
        (`_session_token`). The operator's own session is untouched.
        """
        _require_operator(token, "view an agency as its owner")
        session = _session_of(authorization)
        store = store_factory()
        try:
            try:
                secret = store.create_view_session(session.user_id, org_id)
            except UnknownEntityError as exc:
                raise HTTPException(status_code=404, detail=f"no org {org_id!r}") from exc
            except NotOperatorError as exc:
                raise HTTPException(status_code=403, detail=str(exc)) from exc
            except InvalidAccountError as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            viewing = store.session(secret)
        finally:
            store.close()
        assert viewing is not None
        return {"session": secret, "expires_at": viewing.expires_at, "org": org_id}

    @app.get("/api/users")
    def list_users(token: Token | None = needs_read) -> Any:
        """Everyone with an account, and where each of them belongs."""
        _require_operator(token, "list everyone with an account")
        store = store_factory()
        try:
            people = [
                {
                    "id": user.id,
                    "email": user.email,
                    "name": user.name,
                    "created_at": user.created_at,
                    "disabled_at": user.disabled_at,
                    "memberships": [
                        {"org": m.org, "client": m.client, "role": m.role, "since": m.created_at}
                        for m in store.memberships_for(user.id)
                    ],
                }
                for user in store.list_users()
            ]
        finally:
            store.close()
        return {"users": people}

    @app.get("/api/fleet/boxes")
    def list_fleet_boxes(all_: bool = False, token: Token | None = needs_read) -> Any:
        """Every org's agents, which `GET /api/boxes` never is: that one is the caller's org's."""
        _require_operator(token, "list every org's agents")
        store = store_factory()
        try:
            boxes = store.list_boxes()
            if not all_:
                boxes = [b for b in boxes if not is_terminal("box", b.status)]
            return _box_listing(store, boxes, token)
        finally:
            store.close()

    @app.get("/api/orgs/{org_id}/members")
    def list_members(org_id: str, token: Token | None = needs_admin) -> Any:
        """Who is in an org, as what: the team page."""
        _admin_of(token, org_id)
        store = store_factory()
        try:
            _org_or_404(store, org_id)
            members = []
            for m in store.members_of(org_id):
                user = store.get_user(m.user_id)
                assert user is not None
                members.append(
                    {
                        "user": {"id": user.id, "email": user.email, "name": user.name},
                        "role": m.role,
                        "client": m.client,
                        "since": m.created_at,
                    }
                )
        finally:
            store.close()
        return {"org": org_id, "members": members}

    @app.delete("/api/orgs/{org_id}/members/{user_id}")
    def remove_member(org_id: str, user_id: str, token: Token | None = needs_admin) -> Any:
        """Take someone out of an org. Their sessions there end with it.

        Not its last owner: an org nobody can administer is one only the
        operator can fix.
        """
        _admin_of(token, org_id)
        store = store_factory()
        try:
            _org_or_404(store, org_id)
            removed = store.remove_membership(user_id, org_id)
        except UnknownEntityError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except LastOwnerError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        finally:
            store.close()
        return {"removed": {"user": removed.user_id, "org": org_id, "role": removed.role}}

    @app.post("/api/orgs/{org_id}/invites", status_code=202)
    def invite(org_id: str, body: dict[str, Any], token: Token | None = needs_admin) -> Any:
        """Email someone a link that joins `org_id`: `{"email", "role", "client"?}`.

        A pending org may invite (its team should be there when it is
        approved), a few at a time; a declined one may not. Sent before the
        answer, unlike a sign-in link: the person inviting is told if it did
        not go, rather than waiting on an email that never comes.
        """
        _admin_of(token, org_id)
        if deliver_link is None:
            raise HTTPException(
                status_code=503,
                detail="this control plane cannot send email; set $FLOTTA_RESEND_API_KEY",
            )
        try:
            email = normalize_email(str(body.get("email") or ""))
        except InvalidAccountError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        role = str(body.get("role") or "")
        client = body.get("client")
        client = str(client).strip() if client is not None else None
        store = store_factory()
        try:
            org = _org_or_404(store, org_id)
            invitee = store.get_user_by_email(email)
            if invitee is not None and store.membership(invitee.id, org_id) is not None:
                raise HTTPException(status_code=409, detail=f"{email} is already in {org_id!r}")
            org_limit = invites_per_pending_org if org.status == "pending" else invites_per_org
            if not hit_all((per_address, email), (org_limit, org_id)):
                raise HTTPException(
                    status_code=429,
                    detail="too many invites sent; wait, then send again",
                    headers={"Retry-After": str(int(LINK_LIMIT_WINDOW_S))},
                )
            try:
                secret = store.create_login_link(
                    email,
                    purpose="invite",
                    org_id=org_id,
                    client_id=client,
                    role=role,
                    ttl_s=INVITE_TTL_S,
                )
            except InvalidAccountError as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc
            except UnknownEntityError as exc:
                raise HTTPException(status_code=404, detail=str(exc)) from exc
            except OrgNotActiveError as exc:
                raise HTTPException(status_code=403, detail=str(exc)) from exc
        finally:
            store.close()
        try:
            deliver_link(invite_message(email, sign_in_url(link_base, secret), org.name, role))
        except Exception as exc:  # noqa: BLE001 - said to the inviter, not swallowed
            _log.exception("could not deliver an invite to %s", email)
            raise HTTPException(
                status_code=502, detail=f"the invite to {email} could not be sent; try again"
            ) from exc
        return {"invited": email, "org": org_id, "role": role, "client": client}

    @app.get("/api/orgs/{org_id}/clients")
    def list_org_clients(org_id: str, token: Token | None = needs_read) -> Any:
        """An org's clients — or, for a token confined to one, that one."""
        from dataclasses import asdict

        if token is not None and not _is_operator(token) and (token.org or DEFAULT_ORG) != org_id:
            raise HTTPException(status_code=404, detail=f"no org {org_id!r}")
        store = store_factory()
        try:
            _org_or_404(store, org_id)
            clients = store.list_clients(org_id)
            if token is not None and token.client is not None:
                clients = [c for c in clients if c.id == token.client]
            return {"org": org_id, "clients": [asdict(c) for c in clients]}
        finally:
            store.close()

    @app.post("/api/orgs/{org_id}/clients", status_code=201)
    def create_org_client(
        org_id: str, body: dict[str, Any], token: Token | None = needs_admin
    ) -> Any:
        """Add one of an org's customers: `{"id": "acme", "name": "Acme"}`."""
        _admin_of(token, org_id)
        from dataclasses import asdict

        from flotta.store import DuplicateTenantError, InvalidTenantError

        store = store_factory()
        try:
            _org_or_404(store, org_id)
            client = store.create_client(
                org_id, str(body.get("id") or ""), str(body.get("name") or "")
            )
        except InvalidTenantError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except DuplicateTenantError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        finally:
            store.close()
        return {"client": asdict(client)}

    @app.post("/api/orgs/{org_id}/tokens", status_code=201)
    def mint_org_token(org_id: str, body: dict[str, Any], token: Token | None = needs_admin) -> Any:
        """A token for one of an org's people: `{"subject", "role", "client"?, "days"?}`.

        By role, never by scope list: an org admin hands out what a person in
        the org may be, and cannot reach for the machine scopes (`box:peer`,
        `git:credential`, `fleet:route`) that only boxes and the door hold.

        Shown once, in this response; nothing is stored, because revocation is
        rotating the key (D-log, M5a) and a stored token would be one more copy.
        """
        _admin_of(token, org_id)
        if not key:
            raise HTTPException(
                status_code=503,
                detail=f"no ${SIGNING_KEY_ENV} configured on the control plane; "
                "it cannot mint tokens without one",
            )
        subject = str(body.get("subject") or "").strip()
        if not subject:
            raise HTTPException(status_code=422, detail="who is this token for? name a subject")
        if subject_box(subject) is not None or subject == "relay":
            raise HTTPException(
                status_code=422,
                detail=f"{subject!r} names a machine; a box's token is minted when it is created",
            )
        # `mint` refuses this too; checked here so it is a 422 and not a 500.
        if subject.startswith(USER_SUBJECT_PREFIX):
            raise HTTPException(
                status_code=422,
                detail=f"{subject!r} names a person who signs in; they get a session, not a token",
            )
        client = body.get("client")
        client = str(client).strip() if client is not None else None
        days = body.get("days", 30)
        if not isinstance(days, int) or isinstance(days, bool) or not 1 <= days <= 365:
            raise HTTPException(status_code=422, detail="days must be a whole number from 1 to 365")
        try:
            scopes = scopes_for_role(str(body.get("role") or ""), client=client)
        except AuthError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

        store = store_factory()
        try:
            _org_or_404(store, org_id)
            if client is not None and store.get_client(org_id, client) is None:
                raise HTTPException(
                    status_code=404, detail=f"org {org_id!r} has no client {client!r}"
                )
        finally:
            store.close()

        value = mint(
            subject=subject,
            scopes=scopes,
            ttl_s=days * 24 * 3600,
            key=key,
            # The default org's tokens carry no org claim, exactly as before.
            org=None if org_id == DEFAULT_ORG else org_id,
            client=client,
        )
        claims = verify(value, key=key)
        return {
            "token": value,
            "subject": claims.subject,
            "org": org_id,
            "client": client,
            "role": body["role"],
            "scopes": sorted(claims.scopes),
            "expires_at": claims.expires_at,
        }

    # -- the caller, and their sessions (M12.2) -----------------------------

    @app.get("/api/me")
    def me(
        token: Token | None = needs_anyone, authorization: str | None = Header(default=None)
    ) -> Any:
        """Who this request is, and what it may do.

        For a session, also the person, every org they belong to (the web app's
        org switcher) and the session itself. For a token, its claims only: a
        token names whoever it was minted for, not an account.
        """
        if token is None:
            return {"authenticated": False, "operator": True, "user": None, "session": None}
        status_store = store_factory()
        try:
            acting = status_store.get_org(token.org or DEFAULT_ORG)
        finally:
            status_store.close()
        payload: dict[str, Any] = {
            "authenticated": True,
            "operator": _is_operator(token),
            "subject": token.subject,
            "org": token.org or DEFAULT_ORG,
            # `pending` until the operator approves a sign-up: the web app's
            # "waiting for approval", and why creating an agent is refused.
            "org_status": acting.status if acting else None,
            "client": token.client,
            "scopes": sorted(token.scopes),
            "expires_at": token.expires_at,
            "user": None,
            "memberships": [],
            "session": None,
            # The operator looking at this org as its owner: nothing may change.
            "viewing_as": False,
        }
        if (authorization or "").partition(" ")[0].lower() != "session":
            return payload
        session = _session_of(authorization)
        store = store_factory()
        try:
            user = store.get_user(session.user_id)
            memberships = store.memberships_for(session.user_id)
        finally:
            store.close()
        assert user is not None  # `_session_token` refused this request otherwise
        payload["user"] = {"id": user.id, "email": user.email, "name": user.name}
        payload["memberships"] = [
            {"org": m.org, "client": m.client, "role": m.role} for m in memberships
        ]
        payload["session"] = _session_view(session, current=session.id_hash)
        payload["viewing_as"] = session.read_only
        return payload

    def _session_view(session: Any, *, current: str) -> dict[str, Any]:
        """A session as its owner sees it. Its id is the hash, never the secret."""
        return {
            "id": session.id_hash,
            "org": session.org,
            "created_at": session.created_at,
            "expires_at": session.expires_at,
            "current": session.id_hash == current,
            "read_only": session.read_only,
        }

    @app.post("/api/auth/sign-out")
    def sign_out(
        token: Token | None = needs_anyone, authorization: str | None = Header(default=None)
    ) -> Any:
        """End the session this request was made with."""
        session = _session_of(authorization)
        store = store_factory()
        try:
            store.revoke_session(session.id_hash)
        finally:
            store.close()
        return {"signed_out": True, "subject": token.subject if token else None}

    @app.get("/api/sessions")
    def list_sessions(
        token: Token | None = needs_anyone, authorization: str | None = Header(default=None)
    ) -> Any:
        """The caller's live sessions, in every org, newest first."""
        session = _session_of(authorization)
        assert token is not None and token.subject.startswith(USER_SUBJECT_PREFIX)
        store = store_factory()
        try:
            mine = store.sessions_for(session.user_id)
        finally:
            store.close()
        return {"sessions": [_session_view(s, current=session.id_hash) for s in mine]}

    @app.delete("/api/sessions/{session_id}")
    def revoke_session(
        session_id: str,
        token: Token | None = needs_anyone,
        authorization: str | None = Header(default=None),
    ) -> Any:
        """End one of the caller's own sessions: a browser left signed in elsewhere.

        Someone else's session is the 404 of no session, as a box is.
        """
        session = _session_of(authorization)
        assert token is not None and token.subject.startswith(USER_SUBJECT_PREFIX)
        store = store_factory()
        try:
            if session_id not in {s.id_hash for s in store.sessions_for(session.user_id)}:
                raise HTTPException(status_code=404, detail=f"no session {session_id!r}")
            store.revoke_session(session_id)
        finally:
            store.close()
        return {"revoked": session_id}

    # -- signing in by emailed link (M12.3) ---------------------------------
    #
    # Two open routes, called by the web app's server. Asking for a link
    # proves nothing, so it grants nothing and says nothing; redeeming one
    # proves the inbox, and the session it creates is that address's alone.

    @app.post("/api/auth/link", status_code=202)
    def request_link(
        body: dict[str, Any],
        request: Request,
        tasks: BackgroundTasks,
        authorization: str | None = Header(default=None),
    ) -> Any:
        """Email a sign-in link to `email`, if it can sign in. Always `202`.

        The same answer, the same limits and the same timing whether or not
        there is an account: the account lookup, the link and the email all
        happen after the response, so nothing here tells anyone who has one.
        """
        try:
            email = normalize_email(str(body.get("email") or ""))
        except InvalidAccountError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        caller = _link_caller(request, authorization)
        if not hit_all((per_address, email), (per_ip, caller)):
            raise HTTPException(
                status_code=429,
                detail="too many sign-in links asked for; wait, then ask again",
                headers={"Retry-After": str(int(LINK_LIMIT_WINDOW_S))},
            )
        tasks.add_task(_send_link, email)
        return {"sent": True}

    @app.post("/api/auth/sign-up", status_code=202)
    def sign_up(
        body: dict[str, Any],
        request: Request,
        tasks: BackgroundTasks,
        authorization: str | None = Header(default=None),
    ) -> Any:
        """Email a link that creates an agency: `{"email", "org", "org_name"}`.

        The org is made when the link is redeemed, which proves the address,
        and made `pending`: it can sign in and invite its team, and creates no
        agent until the operator approves it.

        Like asking for a sign-in link, always `202` and the same limits, so
        it says nothing about who has an account; someone who already belongs
        to an org gets a sign-in link instead. What it does say is whether the
        org id is free, which is no secret: org ids are in agents' hostnames.
        """
        try:
            email = normalize_email(str(body.get("email") or ""))
        except InvalidAccountError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        from flotta.store import _tenant_name, validate_tenant_id

        try:
            org_id = validate_tenant_id(str(body.get("org") or ""), kind="org")
            org_name = _tenant_name(str(body.get("org_name") or ""), kind="org")
        except InvalidTenantError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        store = store_factory()
        try:
            if store.get_org(org_id) is not None:
                raise HTTPException(status_code=409, detail=f"an org {org_id!r} already exists")
        finally:
            store.close()
        caller = _link_caller(request, authorization)
        if not hit_all((per_address, email), (per_ip, caller)):
            raise HTTPException(
                status_code=429,
                detail="too many sign-in links asked for; wait, then ask again",
                headers={"Retry-After": str(int(LINK_LIMIT_WINDOW_S))},
            )
        tasks.add_task(_send_sign_up, email, org_id, org_name)
        return {"sent": True}

    def _send_sign_up(email: str, org_id: str, org_name: str) -> None:
        """A sign-up link, or a sign-in link for someone who has an org already."""
        if deliver_link is None:
            return
        store = store_factory()
        try:
            user = store.get_user_by_email(email)
            if user is not None and user.disabled_at is not None:
                return
            if user is not None and store.memberships_for(user.id):
                message = sign_in_message(
                    user.email, sign_in_url(link_base, store.create_login_link(user.email))
                )
            else:
                try:
                    secret = store.create_login_link(
                        email, purpose="sign_up", org_id=org_id, org_name=org_name
                    )
                except DuplicateTenantError:
                    return  # taken since the 202; the redeem would refuse it anyway
                message = sign_up_message(email, sign_in_url(link_base, secret))
        finally:
            store.close()
        try:
            deliver_link(message)
        except Exception:  # noqa: BLE001 - the caller already has its 202
            _log.exception("could not deliver a sign-up link to %s", email)

    def _link_caller(request: Request, authorization: str | None) -> str:
        """Whom the per-caller limit counts: the browser the web app names, or the peer."""
        vouched = request.headers.get(CALLER_IP_HEADER)
        if vouched is None:
            return caller_address(
                request.client.host if request.client else None,
                request.headers.getlist("x-forwarded-for"),
                proxy_hops,
            )
        token = check_relay(request, authorization)
        if not _is_operator(token):
            raise HTTPException(
                status_code=403,
                detail=f"only the operator's token may name the caller in {CALLER_IP_HEADER}",
            )
        try:
            return str(ipaddress.ip_address(vouched.strip()))
        except ValueError as exc:
            raise HTTPException(
                status_code=422, detail=f"{CALLER_IP_HEADER} is not an address: {vouched!r}"
            ) from exc

    def _send_link(email: str) -> None:
        """Make and deliver a link, for an account that could use one."""
        if deliver_link is None:
            return
        store = store_factory()
        try:
            user = store.get_user_by_email(email)
            if user is None or user.disabled_at is not None:
                return
            # No membership, no session to sign in to: that is a sign-up.
            if not store.memberships_for(user.id):
                return
            secret = store.create_login_link(user.email)
        finally:
            store.close()
        try:
            deliver_link(sign_in_message(user.email, sign_in_url(link_base, secret)))
        except Exception:  # noqa: BLE001 - the caller already has its 202
            _log.exception("could not deliver a sign-in link to %s", user.email)

    @app.post("/api/auth/redeem")
    def redeem_link(body: dict[str, Any]) -> Any:
        """Trade a link's secret for a session: once, within its lifetime.

        A sign-in link signs in to the person's first org, or `org` if they
        name one of theirs. An invite joins its org and signs in there; a
        sign-up creates its org, `pending`, and signs in there. `purpose` in
        the answer says which happened. Every reason a link opens nothing is
        the same 401, so a guess and a stale link look alike.
        """
        refused = HTTPException(
            status_code=401, detail="this link has expired or was already used; ask for a new one"
        )
        wanted = body.get("org")
        offered = str(body.get("secret") or "")
        store = store_factory()
        try:
            # Everything is checked against a look at the link, and only then
            # is it spent: a refusal that used it up would leave someone who
            # named the wrong org holding a dead link.
            link = store.login_link(offered)
            if link is None:
                raise refused
            if link.purpose == "invite":
                return _join(store, link, offered, refused)
            if link.purpose == "sign_up":
                return _create(store, link, offered, refused)
            if link.purpose != "sign_in":
                raise refused
            user = store.get_user_by_email(link.email)
            if user is None or user.disabled_at is not None:
                raise refused
            orgs = [m.org for m in store.memberships_for(user.id)]
            if not orgs:
                raise refused
            org = str(wanted) if wanted is not None else orgs[0]
            if org not in orgs:
                raise HTTPException(
                    status_code=403,
                    detail=f"you are not a member of {org!r}; the link still works for {orgs}",
                )
            # The one that counts: two redeems may both have got this far.
            if store.redeem_login_link(offered) is None:
                raise refused
            try:
                secret = store.create_session(user.id, org)
            except (InvalidAccountError, UnknownEntityError) as exc:
                raise refused from exc
            session = store.session(secret)
        finally:
            store.close()
        assert session is not None
        return _signed_in(user, session, secret, purpose="sign_in")

    def _join(store: Any, link: Any, offered: str, refused: HTTPException) -> Any:
        """An invite: the membership it names, then a session in that org."""
        user = store.get_user_by_email(link.email)
        if user is not None and user.disabled_at is not None:
            raise refused
        org = store.get_org(link.org)
        # Declined since it was sent: the decline is the later word.
        if org is None or org.status == "declined":
            raise refused
        if store.redeem_login_link(offered) is None:
            raise refused
        try:
            user, _membership = store.accept_invite(link)
            secret = store.create_session(user.id, link.org)
        except (InvalidAccountError, UnknownEntityError) as exc:
            raise refused from exc
        session = store.session(secret)
        assert session is not None
        return _signed_in(user, session, secret, purpose="invite")

    def _create(store: Any, link: Any, offered: str, refused: HTTPException) -> Any:
        """A sign-up: a pending org with this person as its owner, signed in."""
        user = store.get_user_by_email(link.email)
        if user is not None and user.disabled_at is not None:
            raise refused
        if store.get_org(link.org) is not None:
            raise HTTPException(
                status_code=409,
                detail=f"an org {link.org!r} was created since this link was sent; sign up again",
            )
        if store.redeem_login_link(offered) is None:
            raise refused
        try:
            user, _org, _membership = store.sign_up(link.email, link.org, link.org_name)
            secret = store.create_session(user.id, link.org)
        except InvalidAccountError as exc:
            raise refused from exc
        except DuplicateTenantError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        session = store.session(secret)
        assert session is not None
        return _signed_in(user, session, secret, purpose="sign_up")

    def _signed_in(user: Any, session: Any, secret: str, *, purpose: str) -> dict[str, Any]:
        """A new session, for the web app's server to put in its cookie."""
        return {
            "session": secret,
            "id": session.id_hash,
            "org": session.org,
            "expires_at": session.expires_at,
            "purpose": purpose,
            "user": {"id": user.id, "email": user.email, "name": user.name},
        }

    @app.post("/api/auth/switch")
    def switch_org(
        body: dict[str, Any],
        token: Token | None = needs_anyone,
        authorization: str | None = Header(default=None),
    ) -> Any:
        """A session in another of one's orgs, ending this one: the org switcher.

        An org one is not in is a 404, as another org's box is, and the
        current session is kept.
        """
        session = _session_of(authorization)
        assert token is not None and token.subject.startswith(USER_SUBJECT_PREFIX)
        org = str(body.get("org") or "")
        store = store_factory()
        try:
            user = store.get_user(session.user_id)
            try:
                secret = store.create_session(session.user_id, org)
            except UnknownEntityError as exc:
                raise HTTPException(status_code=404, detail=f"no org {org!r}") from exc
            except InvalidAccountError as exc:
                raise HTTPException(status_code=401, detail=str(exc)) from exc
            store.revoke_session(session.id_hash)
            switched = store.session(secret)
        finally:
            store.close()
        assert user is not None and switched is not None
        return _signed_in(user, switched, secret, purpose="switch")

    @app.post("/api/users", status_code=201)
    def add_user(body: dict[str, Any], token: Token | None = needs_admin) -> Any:
        """Give a person a role in an org, creating their account if need be.

        The operator's, for the first people on a fleet: the first operator
        (an owner of the default org) and an agency's first owner. An owner
        adding their own team sends an invite (`POST /api/orgs/{id}/invites`),
        which the invited person accepts; this is not that.
        """
        _require_operator(token, "add people directly")
        client = body.get("client")
        store = store_factory()
        try:
            user, membership, created = store.add_person(
                str(body.get("email") or ""),
                str(body.get("org") or ""),
                str(body.get("role") or ""),
                client_id=str(client).strip() if client is not None else None,
                name=body.get("name"),
            )
        except InvalidAccountError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except UnknownEntityError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except DuplicateMembershipError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        finally:
            store.close()
        return {
            "user": {"id": user.id, "email": user.email, "name": user.name},
            "membership": {
                "org": membership.org,
                "client": membership.client,
                "role": membership.role,
            },
            "created": created,
        }

    @app.get("/api/settings")
    def get_settings(token: Token | None = needs_read) -> Any:
        """What the fleet is configured with, and where each value came from.

        The catalogue travels with the values so the app renders a form without
        hardcoding one: a setting added here appears in the window without the
        app being rebuilt, which is the point of having a catalogue at all.

        **No secret is reachable through this.** `flotta.settings.SETTINGS` is
        an allowlist of configuration, and the signing key, the GitHub token and
        the box password are not in it — by construction, not by filtering.
        """
        _require_operator(token, "read the fleet's settings")
        from flotta.settings import describe

        store = store_factory()
        try:
            return {"settings": describe(store)}
        finally:
            store.close()

    @app.put("/api/settings")
    def put_settings(body: dict[str, Any], token: Token | None = needs_write) -> Any:
        """Change fleet settings. `{"values": {"KEY": "value", ...}}`.

        An empty string clears an override, which is what an emptied field in
        the app means — the value falls back to the environment, or to the
        default. That is the same verb as "reset this", so there is no separate
        delete endpoint to get out of step with it.

        Values are validated *before* anything is written, and the whole
        request is refused if any of them is bad. A partial apply would leave
        the fleet in a state nobody asked for and the form showing something
        else.
        """
        _require_operator(token, "change the fleet's settings")
        from flotta.settings import UnknownSettingError, describe, validate

        values = body.get("values")
        if not isinstance(values, dict):
            raise HTTPException(status_code=422, detail="expected {'values': {key: value}}")

        cleaned: dict[str, str] = {}
        for key, raw in values.items():
            try:
                cleaned[str(key)] = validate(str(key), "" if raw is None else str(raw))
            except UnknownSettingError as exc:
                # A key nobody may set. 422 rather than 403: the caller has the
                # right scope, they named something that is not a setting.
                raise HTTPException(status_code=422, detail=str(exc)) from exc
            except ValueError as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc

        store = store_factory()
        try:
            # All or nothing, so "refused as a whole" covers a database failure
            # and not only bad input.
            store.set_settings(cleaned)
            return {"settings": describe(store)}
        finally:
            store.close()

    @app.get("/api/hermes")
    def hermes_versions(token: Token | None = needs_read) -> Any:
        """Which Hermes the fleet would build next, and whether a newer exists.

        Three different facts have been one sentence until now, and the app
        needs them apart:

        - **`pinned`** — what the *next* image would be built from. It is this
          control plane's own `HERMES_REF`, so it says nothing about any
          running agent.
        - **`latest`** — the newest Hermes there is. `None` when GitHub could
          not be reached, which must read as *unknown*: reporting "up to date"
          on a failed check hides a real upgrade, and "behind" nags people into
          rebuilding for nothing.
        - **`fleet_image`** — the image an upgrade with no argument would move
          an agent onto. Reported rather than assumed correct: it is a
          deployment variable, and a stale one points at an app that may not
          exist any more.

        What an individual agent runs is none of these. That is the label on
        the image it boots, and it comes back from
        `GET /api/boxes/{id}/machine`.
        """
        _require_operator(token, "read the fleet's Hermes versions")
        from flotta.hermes import report
        from flotta.provision import _newest_release_image, resolve_fleet_image

        store = store_factory()
        try:
            built = store.last_successful_build()
        finally:
            store.close()

        found = report(fleet_ref=built.hermes_ref if built else None)
        fleet_image, source = resolve_fleet_image()

        # What the build app last released, reported even when the environment
        # won — because the two disagreeing *is* the trap. A deployment
        # variable naming an image older than the newest build makes the app
        # offer the wrong upgrade with total confidence, and there is nowhere
        # else a person could notice.
        newest = None
        app = (os.environ.get("FLOTTA_FLY_APP") or "").strip() or None
        if app:
            newest = _newest_release_image(app)

        return {
            "pinned": found.pinned,
            #: What the fleet's newest image was built at, and where that came
            #: from. `pinned` is an intention; this is what exists. They
            #: diverge permanently after the first update started from the app,
            #: which builds at a given ref and never edits source.
            "fleet_ref": found.fleet_ref,
            "fleet_ref_source": found.fleet_ref_source,
            "latest": found.latest,
            "behind": found.behind,
            "unavailable": found.unavailable,
            "fleet_image": fleet_image,
            #: `env`, `release`, or `none`. "Why is my fleet not using the
            #: image I just built" is otherwise unanswerable from a window.
            "fleet_image_source": source,
            "newest_release": newest,
            #: Which app releases are read from, or `null` when none is
            #: configured. Without this, `source: "none"` has two causes with
            #: two different fixes — no app configured, and an app configured
            #: whose releases cannot be read — and they are indistinguishable
            #: from outside. That ambiguity cost a live debugging round: the
            #: answer was the same either way, so the only way to tell was to
            #: go and look at the deployment's variables.
            "fleet_image_app": app,
        }

    @app.get("/api/hermes/builds")
    def list_builds(token: Token | None = needs_read) -> Any:
        """What the fleet has built, newest first — the app's progress view."""
        _require_operator(token, "read the fleet's image builds")
        store = store_factory()
        try:
            from dataclasses import asdict

            return {"builds": [asdict(b) for b in store.list_builds()]}
        finally:
            store.close()

    @app.post("/api/hermes/update", status_code=202)
    def update_hermes(body: dict[str, Any] | None = None, token: Token | None = needs_write) -> Any:
        """Build the box image at a Hermes ref, then move every agent onto it.

        **This is the button.** Everything else in this API is a piece of it:
        `/api/hermes` says a newer Hermes exists, `images.build_box_image`
        makes an image carrying it, and `upgrade_box` moves an agent without
        touching its disk. Until this endpoint they were three things a person
        did from a terminal, which meant the window could report the problem
        and not fix it.

        **Answers 202 and works on a thread.** A cold image build is minutes
        and a proxy in front of this cuts at 60 seconds — the same reason
        create and upgrade are backgrounded. Progress is a `builds` row plus
        the usual `reimaged` / `upgrade_failed` events on each agent.

        **Agents are rolled one at a time and it stops at the first failure.**
        Not for tidiness: the first agent *is* the canary. A Hermes that will
        not serve should cost one agent a restart, not the whole fleet — and a
        failed upgrade leaves that agent exactly as it was, which is what makes
        stopping a real recovery rather than a half-migrated fleet.
        """
        _require_operator(token, "update every agent's Hermes")
        from fastapi.responses import JSONResponse

        from flotta.box.image import HERMES_REF

        ref = str((body or {}).get("hermes_ref") or "").strip() or HERMES_REF

        from flotta.store import BuildInProgressError

        store = store_factory()
        try:
            # The check lives inside `start_build`'s transaction. Read here and
            # insert after — which is what this did — lets two POSTs pass the
            # same check and both start, because the guard serialises the write
            # and not the decision behind it.
            #
            # Two concurrent updates race for one app's release history, and
            # the loser's agents get rolled onto an image the winner replaced.
            build = store.start_build(ref)
        except BuildInProgressError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        finally:
            store.close()

        def roll() -> None:
            from flotta.images import BuildError, build_box_image
            from flotta.provision import ProvisionError, UpgradeFailed, upgrade_box

            inner = store_factory()
            try:
                app_name = (os.environ.get("FLOTTA_FLY_APP") or "").strip()
                if not app_name:
                    inner.finish_build(
                        build.id,
                        error=(
                            "no build app configured. Set FLOTTA_FLY_APP on the control "
                            "plane to the app the box image is released into."
                        ),
                    )
                    return
                try:
                    image = build_box_image(ref, app=app_name, region=_build_region())
                except BuildError as exc:
                    inner.finish_build(build.id, error=str(exc))
                    return
                # Not `finish_build`: the operation is not over. Marking it
                # done here made "one update at a time" true for the build
                # minutes and false for the roll — a second update could start
                # while agents were still moving, and the app re-offered its
                # button mid-roll.
                inner.mark_rolling(build.id)

                for box in inner.list_boxes():
                    if is_terminal("box", box.status) or box.status == "provisioning":
                        continue
                    try:
                        upgrade_box(box.id, store=inner, image=image, reason="hermes-update")
                    except (UpgradeFailed, ProvisionError) as exc:
                        # `upgrade_box` has already written `upgrade_failed`
                        # against the box, so the app can say which agent and
                        # why. Stopping here is the point: see the docstring.
                        _log.warning("rolling stopped at %s: %s", box.name, exc)
                        inner.finish_build(
                            build.id, error=f"{box.name} could not be upgraded: {exc}"
                        )
                        return

                inner.finish_build(build.id, image=image)
            except Exception as exc:  # noqa: BLE001
                with contextlib.suppress(Exception):
                    inner.finish_build(build.id, error=f"{type(exc).__name__}: {exc}")
                _log.warning("hermes update failed: %s: %s", type(exc).__name__, exc)
            finally:
                inner.close()

        worker = threading.Thread(target=roll, name=f"hermes-{ref}", daemon=True)
        _provisioning.add(worker)
        worker.start()

        return JSONResponse(
            {"build_id": build.id, "hermes_ref": ref, "status": "building"}, status_code=202
        )

    @app.get("/api/boxes")
    def list_boxes(all_: bool = False, token: Token | None = needs_read) -> Any:
        store = store_factory()
        try:
            boxes = _scoped_boxes(store, token)
            if not all_:
                # A stopped box is idle, not finished — hiding it would hide
                # the point of the fleet.
                boxes = [b for b in boxes if not is_terminal("box", b.status)]
            return _box_listing(store, boxes, token)
        finally:
            store.close()

    def _box_listing(store: Any, boxes: list[Any], token: Token | None) -> Any:
        """`boxes` as a list answers them, each with its newest task and spend."""
        # Each box's newest task and its total spend. Cost lives on tasks
        # (the formula measures start-to-verdict, meaningless across a
        # machine that spans months), so the list view has to sum it. The
        # direct-SQL version this replaces did `SUM(cost_estimate)`, and
        # dropping it silently turned every cost in the UI into a blank.
        summaries: dict[str, dict[str, Any]] = {}
        for box in boxes:
            tasks = store.list_tasks(box_id=box.id)
            costs = [t.cost_estimate for t in tasks if t.cost_estimate is not None]
            summaries[box.id] = {
                "latest_task": tasks[0].prompt if tasks else None,
                "task_count": len(tasks),
                # None rather than 0.0 when nothing is priced: a blank says
                # "no rate configured", a zero claims the box ran for free.
                "cost_estimate": sum(costs) if costs else None,
            }
        from flotta.provision import fleet_model, model_changes, model_given_by_default

        ids = [b.id for b in boxes]
        metas = store.meta_for_boxes(ids)
        models = store.models_for_boxes(ids)
        given = store.models_given(ids)
        changes = model_changes(store, ids)
        default = fleet_model(store)
        default_given = model_given_by_default()
        return {
            "boxes": [
                _for_reader(
                    {
                        **_box_dict(
                            b,
                            metas.get(b.id),
                            model=models.get(b.id),
                            fleet_model=default,
                            given=given.get(b.id) or default_given,
                            model_change=changes.get(b.id),
                        ),
                        **summaries[b.id],
                    },
                    token,
                )
                for b in boxes
            ]
        }

    @app.post("/api/boxes", status_code=201)
    def create_box_endpoint(body: dict[str, Any], token: Token | None = needs_write) -> Any:
        """Create a box. The API half of "create Agent B" being a button.

        Goes through `provision.create_box` rather than writing a row, for the
        same reason `DELETE` goes through `teardown_box`: the store must never
        claim a machine that was not provisioned. `create_box` also refuses to
        mint a second row for a machine another box already occupies — a guard
        that was unreachable until this endpoint existed to reach it.

        **Whose box (M11.5).** `org` and `client` in the body, each defaulting
        to the token's claim and then to the default org's default client — so
        a token minted for a client creates there without saying so, and every
        request that worked before still creates where it did. A token may
        create only where `admits_tenant` lets it act; anywhere else is the
        same 404 as a client that does not exist, so creation is not a way to
        learn another org's clients.
        """
        from fastapi.responses import JSONResponse

        from flotta.provision import (
            BoxNotRunning,
            BoxOccupied,
            ProvisionError,
            create_box,
        )
        from flotta.store import (
            DuplicateBoxError,
            InvalidBoxNameError,
            InvalidTenantError,
            box_label,
            validate_box_name,
        )

        name = str(body.get("name") or "").strip()
        if not name:
            raise HTTPException(status_code=422, detail="a box needs a name")

        # Here as well as in the store, and deliberately: the store is the
        # guarantee — it is what makes this true for the CLI too — while this
        # is the early exit. Without it a name that cannot work still costs a
        # `flyctl` probe (`_peek_for` runs before the row is reserved) and
        # comes back as a 409 about a duplicate, which is the wrong fix.
        org = str(body.get("org") or "").strip() or (token.org if token else None) or DEFAULT_ORG
        client = (
            str(body.get("client") or "").strip()
            or (token.client if token else None)
            or DEFAULT_CLIENT
        )
        try:
            name = validate_box_name(name)
            # The whole address, so a name too long for its client is a 422
            # here rather than after a probe.
            label = box_label(name, org=org, client=client)
        except (InvalidBoxNameError, InvalidTenantError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        nowhere = HTTPException(status_code=404, detail=f"org {org!r} has no client {client!r}")
        if token is not None and admits_tenant(token, org=org, client=client) is not None:
            raise nowhere
        tenant_store = store_factory()
        try:
            if tenant_store.get_client(org, client) is None:
                raise nowhere
            # Before the occupancy probe, which on Fly is a `flyctl` call: a
            # sign-up that is not approved yet costs nothing. The store refuses
            # the row too (M12.4); this is the early, honest answer.
            try:
                tenant_store.require_active_org(org)
            except OrgNotActiveError as exc:
                raise HTTPException(status_code=403, detail=str(exc)) from exc
        finally:
            tenant_store.close()

        # What makes this agent different from the fleet default. Absent means
        # "the fleet decides", which is the common case and stays a one-field
        # request.
        raw_volume = body.get("volume_gb")
        volume_gb: int | None = None
        if raw_volume not in (None, ""):
            try:
                volume_gb = int(raw_volume)
            except (TypeError, ValueError) as exc:
                raise HTTPException(
                    status_code=422, detail=f"volume_gb must be a whole number, got {raw_volume!r}"
                ) from exc
            if volume_gb <= 0:
                raise HTTPException(
                    status_code=422, detail=f"volume_gb must be at least 1, got {volume_gb}"
                )
        region = (str(body.get("region") or "")).strip() or None

        # Who the agent is. Validated here for the early exit and again in the
        # store for the guarantee; a description that cannot be stored must
        # refuse before a name is spent.
        from flotta.store import InvalidBoxMetaError, validate_box_meta

        try:
            display_name, description, instructions = validate_box_meta(
                body.get("display_name"), body.get("description"), body.get("instructions")
            )
        except InvalidBoxMetaError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

        # Which model, if not the fleet's. Checked before a name is spent: a
        # typo here is an agent that answers every turn with "model not found".
        model = _checked_model(body.get("model"))

        if background:
            return _create_in_background(
                name,
                token=token,
                org=org,
                client=client,
                label=label,
                volume_gb=volume_gb,
                region=region,
                display_name=display_name,
                description=description,
                instructions=instructions,
                model=model,
            )

        store = store_factory()
        try:
            try:
                result = create_box(
                    name,
                    store=store,
                    org=org,
                    client=client,
                    volume_gb=volume_gb,
                    region=region,
                    display_name=display_name,
                    description=description,
                    instructions=instructions,
                    model=model,
                )
            except DuplicateBoxError as exc:
                # A name still held by an existing box. Since teardown releases
                # a destroyed agent's name, this now means a *live* one — which
                # is a conflict the caller can act on, not the bare 500 an
                # uncaught IntegrityError used to produce.
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            except BoxOccupied as exc:
                # The only create failure that is genuinely a *conflict*: the
                # machine is taken, and the caller can act on that.
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            except BoxNotRunning as exc:
                # A box **was** created, and the row says `stopped`. Answering
                # with an error would tell the caller nothing was made while a
                # machine sits there costing disk — and they would not even
                # learn its id, so they could neither start it nor destroy it.
                # That is how an orphan machine bills against an account nobody
                # is watching, which this repo has done once already.
                #
                # 201 with the box, plus a warning saying it is not up.
                box = _scoped_box(store, token, exc.box_id)
                return JSONResponse(
                    {
                        "box": _box_dict(box),
                        "box_id": exc.box_id,
                        "endpoint": exc.endpoint,
                        "warning": str(exc),
                    },
                    status_code=201,
                )
            except ProvisionError as exc:
                # Everything else is the substrate failing, not the caller
                # asking for something impossible. 502 matches DELETE, which
                # already reports a failed teardown that way.
                raise HTTPException(status_code=502, detail=str(exc)) from exc
            box = _scoped_box(store, token, result["box_id"])
            return {"box": _with_meta(store, box), **result}
        finally:
            store.close()

    def _create_in_background(
        name: str,
        *,
        token: Token | None,
        org: str,
        client: str,
        label: str,
        volume_gb: int | None = None,
        region: str | None = None,
        display_name: str | None = None,
        description: str | None = None,
        instructions: str | None = None,
        model: str | None = None,
    ) -> Any:
        """Reserve the row, answer, and provision afterwards.

        Provisioning is an app, a volume, a machine and a boot — minutes on a
        cold path. Held open as one request it outlives the proxy in front of
        it: Railway answered `502 Application failed to respond` while the work
        carried on, so the caller was told it had failed and a machine appeared
        anyway.

        Worse, the request being cancelled took `create_box` with it, leaving a
        row at `provisioning` with no endpoint and a real machine nothing could
        address. The reconcile loop closes those now, but not creating them is
        better than sweeping them up.

        So: `202`, the box id, and a row to poll. The work runs on the event
        loop's own task, which no client disconnect can cancel.
        """
        from fastapi.responses import JSONResponse

        from flotta.backend import UnknownBackendError
        from flotta.provision import _default_backend, create_box, reserve_box
        from flotta.store import DuplicateBoxError

        store = store_factory()
        try:
            try:
                impl = _default_backend()
            except UnknownBackendError as exc:
                # Checked at start; this is the variable changing underneath.
                raise HTTPException(status_code=503, detail=str(exc)) from exc
            # By address: that is what the Fly app is named after.
            probe = _peek_for(impl, label)
            occupied = _occupant(store, token, probe) if probe else None
            if occupied:
                raise HTTPException(status_code=409, detail=occupied)
            try:
                box = reserve_box(
                    name,
                    store=store,
                    backend=impl,
                    org=org,
                    client=client,
                    display_name=display_name,
                    description=description,
                    instructions=instructions,
                    model=model,
                )
            except DuplicateBoxError as exc:
                # A *live* box holds the name — a destroyed one no longer does,
                # because teardown releases it.
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            except Exception as exc:
                raise HTTPException(
                    status_code=409,
                    detail=f"cannot create a box named {name!r}: {exc}",
                ) from exc
            # This row goes straight into the app's sidebar without waiting
            # for a poll, so it has to carry the display name or the agent
            # shows up under its address for five seconds.
            payload = {"box": _with_meta(store, box), "box_id": box.id, "status": box.status}
        finally:
            store.close()

        def provision() -> None:
            inner = store_factory()
            try:
                create_box(name, store=inner, box=box, volume_gb=volume_gb, region=region)
            except Exception as exc:  # noqa: BLE001
                # `create_box` already records the failure against the row;
                # this only stops a stray exception ending the thread in
                # silence.
                _log.warning("provisioning %s failed: %s: %s", name, type(exc).__name__, exc)
            finally:
                inner.close()

        # A thread rather than an asyncio task: this endpoint is a sync `def`,
        # so FastAPI runs it in a threadpool where there is no running loop and
        # `create_task` raises. Capturing the loop at startup would work and is
        # more machinery than the job needs — `create_box` is blocking
        # subprocess work from end to end, so it belongs on a thread either
        # way.
        #
        # If the process dies mid-provision the row is left at `provisioning`
        # with a machine possibly created. That is not ignored: `reconcile_boxes`
        # sweeps exactly that, which is why it was built alongside this.
        worker = threading.Thread(target=provision, name=f"provision-{label}", daemon=True)
        _provisioning.add(worker)
        worker.start()

        return JSONResponse(payload, status_code=202)

    @app.get("/api/boxes/{box_id}")
    def get_box(box_id: str, token: Token | None = needs_read) -> Any:
        store = store_factory()
        try:
            box = _scoped_box(store, token, box_id)
            # Only on the single-box read, never the list: it is one query per
            # box, and the list is the hot path that renders the sidebar on a
            # timer. The one consumer — deciding whether to resume a
            # conversation or start a fresh one — is opening a single agent.
            return {
                "box": _for_reader(
                    {
                        **_with_meta(store, box),
                        "instructions_changed_at": _epoch(store.instructions_changed_at(box.id)),
                    },
                    token,
                ),
                "tasks": [_task_dict(t) for t in store.list_tasks(box_id=box.id)],
            }
        finally:
            store.close()

    @app.put("/api/boxes/{box_id}/meta")
    def set_meta(box_id: str, body: dict[str, Any], token: Token | None = needs_write) -> Any:
        """Rename or re-describe an agent. The address never changes.

        **Instructions are deliberately not editable here.** The store row is
        the *record* of what the agent was seeded with; the copy it reads is
        `SOUL.md` on its own volume, which the entrypoint writes once and never
        touches again. Editing the record after creation would make it lie
        about what the agent runs. Changing a live agent's instructions means
        reaching the volume, and that is a separate verb with a restart or an
        exec behind it — not a field on this endpoint.

        **A full replace, not a merge.** Omitting `display_name` or
        `description` clears it. The app always sends both; a CLI `flotta
        meta` would have to as well. Chosen because "PUT the whole identity"
        has one meaning and "PATCH some of it" has several — and the store's
        `test_meta_is_replaced_not_merged` pins it.
        """
        from flotta.store import InvalidBoxMetaError

        store = store_factory()
        try:
            box = _scoped_box(store, token, box_id)
            current = store.meta_for_box(box.id)
            try:
                store.set_box_meta(
                    box.id,
                    display_name=body.get("display_name"),
                    description=body.get("description"),
                    instructions=current.instructions if current else None,
                )
            except InvalidBoxMetaError as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc
            return {"box": _with_meta(store, box)}
        finally:
            store.close()

    @app.put("/api/boxes/{box_id}/instructions")
    def set_instructions(
        box_id: str, body: dict[str, Any], token: Token | None = needs_write
    ) -> Any:
        """Rewrite what an agent *is*, on the volume it reads.

        Separate from `PUT …/meta` because it is a different kind of act. A
        display name is a label the fleet keeps; standing instructions are a
        file on a machine, so this one wakes a box, writes to its disk, and can
        fail in ways renaming cannot.

        **It does not take effect until the conversation restarts.** Hermes
        renders a system prompt once per session, so the app starts a fresh one
        after saving — `changed_at` is what lets it decide, by comparing
        against the session it was about to resume.
        """
        from flotta.provision import ProvisionError
        from flotta.provision import set_instructions as write_soul
        from flotta.store import InvalidBoxMetaError

        store = store_factory()
        try:
            # Resolved here as well as inside, so an unknown agent is a 404
            # rather than the 409 every other refusal uses.
            box = _scoped_box(store, token, box_id)
            try:
                written = write_soul(box.id, body.get("instructions"), store=store)
                # Same shape and the same units as the single-box read, so the
                # window can use the answer without a second fetch.
                return {**written, "changed_at": _epoch(written["changed_at"])}
            except InvalidBoxMetaError as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc
            except ProvisionError as exc:
                # 409, not 500: the fleet is fine and the request was
                # well-formed — this box could not be written to right now.
                raise HTTPException(status_code=409, detail=str(exc)) from exc
        finally:
            store.close()

    @app.get("/api/boxes/{box_id}/events")
    def get_events(box_id: str, token: Token | None = needs_read) -> Any:
        store = store_factory()
        try:
            box = _operated_box(store, token, box_id)
            events = [_event_dict(e) for e in store.get_box_timeline(box.id)]
            if _reads_instructions(token):
                return {"events": events}
            meta = store.meta_for_box(box.id)
            instructions = meta.instructions if meta else None
            return {"events": [_withheld(e, instructions) for e in events]}
        finally:
            store.close()

    @app.get("/api/boxes/{box_id}/machine")
    def get_machine(box_id: str, token: Token | None = needs_read) -> Any:
        """What the substrate says about this box's machine, beside the row.

        Every other read the app makes is the store's *belief*. This is the one
        that asks. Both are returned, unmerged and unreconciled, because the
        disagreements are the interesting part — a row saying `running` about a
        machine Fly stopped during a host drain is a real thing that happens,
        and a single blended answer would hide precisely it.

        **Never 500s on the substrate.** A person clicked "Info"; `flyctl`
        being unreachable, slow, or logged out is an answer to render
        (`unavailable`), not a failed request. The row is always returned, so
        the panel has something to show either way.

        Not polled, and it must not become polled: it is a subprocess per call.
        The list view stays on the store, which is cheap and, for status, kept
        current by every verb that touches a box.
        """
        from flotta.backend import BackendError, backend_for
        from flotta.provision import _fleet_image, _same_image

        store = store_factory()
        try:
            box = _operated_box(store, token, box_id)
            from flotta.provision import last_activity_at

            # When anything last happened to it (FLOTTA-85): the timeline idle
            # sleep reads, so "last active" here and "why did it sleep" agree.
            active = last_activity_at(store, box.id)
            row = _for_reader(
                {
                    **_with_meta(store, box),
                    "last_active_at": active.isoformat() if active else None,
                },
                token,
            )
            if not box.endpoint:
                # Not a failure: a box being built has no machine yet, which is
                # the honest thing to say rather than an error about flyctl.
                return {
                    "box": row,
                    "machine": None,
                    "unavailable": "this agent has no machine yet",
                    "fleet_image": _fleet_image(),
                    "image_current": None,
                }
            try:
                info = backend_for(box.endpoint).inspect(box.endpoint)
            except (BackendError, OSError, NotImplementedError) as exc:
                return {
                    "box": row,
                    "machine": None,
                    "unavailable": str(exc),
                    "fleet_image": _fleet_image(),
                    "image_current": None,
                }
            from dataclasses import asdict

            # Whether this agent is on the image the fleet builds — computed
            # **here**, with `_same_image`, rather than in the app.
            #
            # Fly reports `repo:tag@sha256:…` while `$FLOTTA_FLY_IMAGE` is
            # written without the digest, so string equality never matches.
            # That comparison has been wrong three times in this repo; a second
            # implementation in TypeScript would be the fourth. `None` means
            # one side is unknown, which is not the same as "behind" and must
            # not put an Upgrade button in front of somebody.
            fleet_image = _fleet_image()
            current: bool | None = None
            if fleet_image and info.image:
                current = _same_image(info.image, fleet_image)

            from flotta.provision import connection_of

            return {
                "box": row,
                "machine": asdict(info),
                "unavailable": None,
                "fleet_image": fleet_image,
                "image_current": current,
                # Whether the machine calls this control plane as this box
                # (FLOTTA-98): a stale address fails every box-side call
                # with nothing on screen to say why.
                "connection": connection_of(box, info),
            }
        finally:
            store.close()

    def _usable_repos(store: Any, box: Any) -> list[str]:
        """`box`'s grants, or none for another org's agent (FLOTTA-83).

        A row from before the rule mints nothing, so naming it would be a
        repository the agent is told it has and is refused when it asks.
        """
        return store.repos_for_box(box.id) if reaches_fleet_github(box.org) else []

    @app.get("/api/boxes/{box_id}/repos")
    def list_repos(box_id: str, token: Token | None = needs_read) -> Any:
        """Which repositories a box may use."""
        store = store_factory()
        try:
            box = _operated_box(store, token, box_id)
            return {"box_id": box.id, "name": box.name, "repos": _usable_repos(store, box)}
        finally:
            store.close()

    @app.post("/api/boxes/{box_id}/repos")
    def grant_repo_endpoint(
        box_id: str, body: dict[str, Any], token: Token | None = needs_write
    ) -> Any:
        """Grant a box a repository. Idempotent.

        `fleet:write` rather than `git:credential`: granting is an operator's
        act, minting is the box's. A box holding its own credential scope must
        not be able to widen its own access.

        **Checked against GitHub before it is stored.** A grant can only narrow
        what the fleet token reaches, never widen it, and a grant naming a
        repository that token cannot see is a booby trap: it looks correct in
        the app and fails later on a machine, as GitHub's
        `Write access to repository not granted` — which reads as a write
        problem during a clone and sends the reader to the wrong page.

        Refused on a definite no, **allowed on an uncertain one**. A GitHub
        outage must not stop somebody configuring their fleet, and the check is
        a point-in-time answer regardless: a token's scope can change after the
        grant, so this removes a surprise rather than a possibility.

        **The operator's alone, for the operator's agents** (FLOTTA-83). The
        token every grant spends is the operator's own, so `fleet:write` in an
        agency's org is not enough: that was an agency granting itself the
        operator's repositories. Both refusals come before GitHub is asked.
        """
        from flotta.store import normalise_repo

        store = store_factory()
        try:
            box = _operated_box(store, token, box_id)
            if not reaches_fleet_github(box.org):
                raise HTTPException(status_code=403, detail=AGENCY_GITHUB_UNAVAILABLE)
            _require_operator(token, "grant a repository")
            # Normalised first, because the check asks GitHub about a path: a
            # pasted URL or a `.git` suffix would ask about a repository that
            # does not exist and read as a denial.
            try:
                wanted = normalise_repo(str(body.get("repo") or ""))
            except ValueError as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc

            check = reachable or _default_reachable
            reach = check(wanted, token=(os.environ.get(GITHUB_TOKEN_ENV) or "").strip())
            if reach.refuses:
                raise HTTPException(status_code=422, detail=reach.detail)

            try:
                repo = store.grant_repo(box.id, wanted)
            except ValueError as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc
            store.add_event("box", box.id, "repo_granted", {"repo": repo})
            return {"box_id": box.id, "repos": store.repos_for_box(box.id)}
        finally:
            store.close()

    @app.delete("/api/boxes/{box_id}/repos/{owner}/{name}")
    def revoke_repo_endpoint(
        box_id: str, owner: str, name: str, token: Token | None = needs_write
    ) -> Any:
        """Withdraw a grant. Takes effect on the box's next credential request.

        No redeploy and no restart: the box holds nothing to invalidate, which
        is the point of it not holding a credential.
        """
        store = store_factory()
        try:
            box = _operated_box(store, token, box_id)
            had = store.revoke_repo(box.id, f"{owner}/{name}")
            if had:
                store.add_event("box", box.id, "repo_revoked", {"repo": f"{owner}/{name}"})
            return {"box_id": box.id, "revoked": had, "repos": store.repos_for_box(box.id)}
        finally:
            store.close()

    def _described(store: Any, box: Any, ids: list[str]) -> list[dict[str, Any]]:
        """`box`'s colleagues, named the way a person and a model both need.

        The address and the description together: an agent choosing whom to
        ask needs "Reviewer — backend PRs", and the thing it must then type is
        the name. FLOTTA-40 put the first on the fleet; this is where it pays.

        Looked up among `box`'s own client only, so an id from anywhere else
        names nobody here — the queries already keep to one client; this is
        the second lock on the same door.
        """
        meta = store.meta_for_boxes(ids)
        same_client = {b.id: b for b in store.list_boxes_in(box.org, box.client)}
        out = []
        for pid in ids:
            peer = same_client.get(pid)
            if peer is None:  # pragma: no cover - the queries already exclude these
                continue
            info = meta.get(pid)
            out.append(
                {
                    "id": peer.id,
                    "name": peer.name,
                    "display_name": info.display_name if info else None,
                    "description": info.description if info else None,
                }
            )
        return out

    def _colleagues(store: Any, box: Any) -> dict[str, Any]:
        """Everything the Colleagues panel shows, in one answer."""
        return {
            "box_id": box.id,
            "name": box.name,
            "peers": _described(store, box, store.peers_for_box(box.id)),
            "blocked": _described(store, box, store.blocked_peers_for_box(box.id)),
        }

    def _colleague(store: Any, box: Any, ref: str) -> Any:
        """The live agent `ref` names (id or name) in `box`'s client, or None.

        Looked for there and nowhere else, so every way of naming a colleague —
        a block, a message — answers for another client's agent exactly as for
        one that does not exist.
        """
        same_client = store.list_boxes_in(box.org, box.client)
        peer = next((b for b in same_client if b.id == ref), None) or next(
            (b for b in same_client if b.name == ref), None
        )
        return None if peer is None or is_terminal("box", peer.status) else peer

    def _peer_target(store: Any, box: Any, ref: str) -> Any:
        """The agent `ref` names among `box`'s client — or 404.

        Looked for there and nowhere else: an agent in another client is the
        404 of no agent, or a block would be a way to ask whether another org
        has one.
        """
        ref = ref.strip()
        if not ref:
            raise HTTPException(status_code=422, detail="which agent?")
        peer = _colleague(store, box, ref)
        if peer is None:
            raise HTTPException(status_code=404, detail=f"no agent {ref!r}")
        if peer.id == box.id:
            raise HTTPException(
                status_code=422, detail="an agent never asks itself, so there is nothing to change"
            )
        return peer

    @app.get("/api/boxes/{box_id}/peers")
    def list_peers(box_id: str, token: Token | None = needs_read) -> Any:
        """Who this agent may ask, and who it has been stopped from asking.

        **Everyone, by default** (FLOTTA-65). A new agent is on every list the
        moment it can be reached, with nothing written anywhere.
        """
        store = store_factory()
        try:
            return _colleagues(store, _scoped_box(store, token, box_id))
        finally:
            store.close()

    @app.post("/api/boxes/{box_id}/peer-blocks")
    def block_peer_endpoint(
        box_id: str, body: dict[str, Any], token: Token | None = needs_write
    ) -> Any:
        """Stop this agent asking one other agent. Idempotent, directed.

        **Its own route rather than a changed meaning for `POST /peers`**,
        which under FLOTTA-54 *granted*. A client from before this change
        calling the old route gets an error, not the opposite of what it asked.

        `fleet:write`: changing who an agent may reach is an operator's act in
        both directions. A box carries `box:peer`, which can only use the list.
        """
        store = store_factory()
        try:
            box = _scoped_box(store, token, box_id)
            peer = _peer_target(store, box, str(body.get("peer") or ""))
            if store.block_peer(box.id, peer.id):
                store.add_event("box", box.id, "peer_blocked", {"peer": peer.name})
            return _colleagues(store, box)
        finally:
            store.close()

    @app.delete("/api/boxes/{box_id}/peer-blocks/{peer}")
    def allow_peer_endpoint(box_id: str, peer: str, token: Token | None = needs_write) -> Any:
        """Lift a block. Takes effect on the next message, with no restart —
        the box never held a way to reach the other agent, only permission to
        ask the control plane for one."""
        store = store_factory()
        try:
            box = _scoped_box(store, token, box_id)
            target = _peer_target(store, box, peer)
            if store.allow_peer(box.id, target.id):
                # The name, whichever was sent: the app sends ids, and a
                # timeline reading `peer=b-e0d6…` is one nobody can read.
                store.add_event("box", box.id, "peer_allowed", {"peer": target.name})
            return _colleagues(store, box)
        finally:
            store.close()

    def _checked_model(raw: Any) -> str | None:
        """A model id from a request, validated and looked up — or None.

        None and "" both mean "the fleet default": the common case, and a
        cleared field in the app. Refused on a malformed id, on a model that is
        not on the fleet's list (FLOTTA-85), and on a definite "no such model";
        allowed when the catalogue cannot be asked.

        **The list is for every caller**, the operator included: it is what
        the fleet's key may be spent on, and the operator changes it in the
        settings rather than going around it.
        """
        from flotta.inference import InvalidModel
        from flotta.provision import allowed_model

        if raw is None or (isinstance(raw, str) and not raw.strip()):
            return None
        store = store_factory()
        try:
            model = allowed_model(str(raw), store)
        except InvalidModel as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        finally:
            store.close()
        # The endpoint is the control plane's own configuration, not a stored
        # setting: it travels with the fleet's key, which never leaves the env.
        base_url = (os.environ.get("FLOTTA_MODEL_BASE_URL") or "").strip()
        if base_url:
            if known_model is not None:
                check = known_model
            else:
                from flotta.model_catalog import known_model as check
            verdict = check(model, base_url)
            if verdict.refuses:
                raise HTTPException(status_code=422, detail=verdict.detail)
        return model

    @app.get("/api/models")
    def list_models(token: Token | None = needs_read) -> Any:
        """The models an agent may be given, and which one is the default.

        What a model picker offers (FLOTTA-85). Readable by anyone who can read
        the fleet: which models exist is the operator's choice, not anyone's
        secret. `default` is what `{"model": null}` means.
        """
        from flotta.provision import allowed_models, fleet_model

        store = store_factory()
        try:
            return {"models": allowed_models(store), "default": fleet_model(store)}
        finally:
            store.close()

    @app.put("/api/boxes/{box_id}/model")
    def set_model_endpoint(
        box_id: str, body: dict[str, Any], token: Token | None = needs_write
    ) -> Any:
        """Change the model an agent runs; `{"model": null}` means the fleet's.

        Like renewing an identity: the value is written to the agent's own
        machine, a running agent restarts to take it, and a sleeping one stays
        asleep and takes it when it wakes. `fleet:write`, for the restart.

        **`202`, then a thread** (FLOTTA-99). Writing the value waits for Fly
        to restart a running agent, and the web app gives a call ten seconds:
        answering afterwards made a change that worked arrive as "the control
        plane did not answer". Refusals are answered first; the box's
        `model_change` says it is under way, and the timeline's `model_set` or
        `model_change_failed` how it ended.
        """
        from fastapi.responses import JSONResponse

        from flotta.provision import (
            MODEL_CHANGE_FAILED,
            ProvisionError,
            UpgradeFailed,
            begin_model_change,
            set_model,
        )

        if "model" not in body:
            raise HTTPException(
                status_code=422, detail='which model? Send {"model": null} for the fleet default.'
            )
        model = _checked_model(body.get("model"))
        store = store_factory()
        try:
            box = _scoped_box(store, token, box_id)
            with _model_changes_lock:
                if box.id in _model_changes_running:
                    raise HTTPException(
                        status_code=409,
                        detail=f"{box.name} is already switching model; wait for that to finish.",
                    )
                try:
                    started = begin_model_change(box.id, model, store=store)
                except ProvisionError as exc:
                    raise HTTPException(status_code=409, detail=str(exc)) from exc
                _model_changes_running.add(box.id)
            if not background:
                try:
                    result = set_model(box.id, model, store=store)
                except UpgradeFailed as exc:
                    raise HTTPException(status_code=502, detail=str(exc)) from exc
                except ProvisionError as exc:
                    raise HTTPException(status_code=409, detail=str(exc)) from exc
                finally:
                    _model_changes_running.discard(box.id)
                return {**result, "box": _with_meta(store, _scoped_box(store, token, box.id))}
            answer = {**started, "started": True, "box": _with_meta(store, box)}
        finally:
            store.close()

        def change() -> None:
            inner = store_factory()
            try:
                set_model(box.id, model, store=inner)
            except Exception as exc:  # noqa: BLE001
                # `set_model` records a failure to write the machine; anything
                # else (the box destroyed meanwhile, say) is recorded here, so
                # the window never says "restarting" until it goes stale.
                if not isinstance(exc, UpgradeFailed):
                    inner.add_event(
                        "box",
                        box.id,
                        MODEL_CHANGE_FAILED,
                        {"model": started["model"], "error": str(exc)[:400]},
                    )
                _log.warning("changing %s's model failed: %s", box.name, exc)
            finally:
                _model_changes_running.discard(box.id)
                inner.close()

        worker = threading.Thread(target=change, name=f"model-{box.name}", daemon=True)
        _provisioning.add(worker)
        worker.start()
        return JSONResponse(answer, status_code=202)

    @app.post("/api/boxes/{box_id}/connection")
    def repair_connection_endpoint(box_id: str, token: Token | None = needs_write) -> Any:
        """Point an agent's machine at this control plane, as itself (FLOTTA-98).

        For an agent whose machine still calls an old control plane, or carries
        an old box id — every agent re-registered after the Railway move. See
        `provision.repair_connection`.

        `fleet:write`, like renewing an identity, which this includes: it
        restarts a running agent. The operator reaches any org's agent
        (`_operated_box`), because a broken connection is the fleet's to fix.

        **`202`, then a thread**, for the reason upgrades are: a running agent
        restarts twice, and the web app gives a call ten seconds. The refusals
        a caller can act on are answered first; the outcome is the timeline's
        `connection_repaired` or `connection_repair_failed`.
        """
        from fastapi.responses import JSONResponse

        from flotta.auth import resolve_signing_key
        from flotta.provision import ProvisionError, UpgradeFailed, repair_connection

        store = store_factory()
        try:
            box = _operated_box(store, token, box_id)
            if is_terminal("box", box.status) or box.status == "provisioning" or not box.endpoint:
                raise HTTPException(
                    status_code=409,
                    detail=f"box {box.name} has no settled machine to repair ({box.status}).",
                )
            if not (os.environ.get("FLOTTA_CONTROL_URL") or "").strip():
                raise HTTPException(
                    status_code=409,
                    detail=(
                        "this control plane does not know its own address. Set "
                        "$FLOTTA_CONTROL_URL where it runs, then repair again."
                    ),
                )
            if not resolve_signing_key():
                raise HTTPException(
                    status_code=409,
                    detail=(
                        "no signing key is configured, so there is no identity to issue. "
                        "Set $FLOTTA_SIGNING_KEY where the control plane runs."
                    ),
                )
            if not background:
                try:
                    return repair_connection(box.id, store=store)
                except UpgradeFailed as exc:
                    raise HTTPException(status_code=502, detail=str(exc)) from exc
                except ProvisionError as exc:
                    raise HTTPException(status_code=409, detail=str(exc)) from exc
        finally:
            store.close()

        def repair() -> None:
            inner = store_factory()
            try:
                repair_connection(box.id, store=inner)
            except Exception as exc:  # noqa: BLE001
                # `repair_connection` writes the failure event before raising.
                _log.warning("repairing %s failed: %s: %s", box.name, type(exc).__name__, exc)
            finally:
                inner.close()

        worker = threading.Thread(target=repair, name=f"repair-{box.name}", daemon=True)
        _provisioning.add(worker)
        worker.start()
        return JSONResponse({"box_id": box.id, "name": box.name, "started": True}, status_code=202)

    @app.post("/api/boxes/{box_id}/identity")
    def rotate_identity_endpoint(box_id: str, token: Token | None = needs_write) -> Any:
        """Give an agent a fresh identity token, written to its own machine.

        Replaces a recipe that wrote the token to whichever Fly app `.env`
        named — the image-build app, on the live fleet — so a rotation run from
        a laptop silently did nothing to the agent it named. The control plane
        knows where each agent lives, which is why the verb belongs here and
        the recipe is now a caller of it.

        `fleet:write`, like upgrading: it restarts a running agent. A sleeping
        one stays asleep. Synchronous, because a sleeping agent — most of them —
        answers in seconds, and a running one in about the time it takes to
        restart, which the app waits for.
        """
        from flotta.provision import ProvisionError, UpgradeFailed, rotate_identity

        store = store_factory()
        try:
            box = _scoped_box(store, token, box_id)
            try:
                return rotate_identity(box.id, store=store)
            except UpgradeFailed as exc:
                raise HTTPException(status_code=502, detail=str(exc)) from exc
            except ProvisionError as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
        finally:
            store.close()

    @app.post("/api/boxes/{box_id}/upgrade")
    def upgrade_box_endpoint(
        box_id: str, body: dict[str, Any] | None = None, token: Token | None = needs_write
    ) -> Any:
        """Move an agent onto a new image, keeping its disk.

        `fleet:write` rather than `box:destroy`: this is the *opposite* of
        destroying — the volume is what it exists to preserve — and gating it
        behind the destroy scope would mean handing out the ability to delete
        an agent in order to update one.

        **Synchronous, and that is a known limit rather than a decision.**
        `flyctl machine update` waits up to 300s by default, and a proxy in
        front of this cut `POST /api/boxes` at 60s once already — answering
        `502 Application failed to respond` while the work carried on. The same
        will happen here for an upgrade that pulls a large image, and the reply
        will be wrong in the same way.

        **Backgrounded, because there is an Upgrade button now.** The note this
        docstring used to carry — "anything that puts this behind a UI should
        background it first, the way FLOTTA-27 did for create" — was the
        instruction, and this is it being followed. `flyctl machine update`
        waits up to 300s, and a proxy in front of this cut `POST /api/boxes` at
        60s once already, answering `502 Application failed to respond` while
        the work carried on.

        What stays synchronous is everything the caller can *act* on: no such
        box, a terminal or mid-provision one, no image to move to. Those are
        409s worth having in the reply. Everything after is the substrate
        taking as long as it takes, and the answer is `202` plus a timeline to
        watch — `reimaged` on success, `upgrade_failed` on failure, both
        written by `upgrade_box`.

        `background=False` keeps the synchronous path, which is what the tests
        assert against and what `flotta upgrade` gets when it talks to a local
        store.
        """
        from flotta.provision import ProvisionError, UpgradeFailed, upgrade_box

        image = str((body or {}).get("image") or "").strip() or None
        if image is not None and not _is_operator(token):
            # Before any box is read. An image is code that boots with the
            # fleet's secrets on the operator's account; an agency moves onto
            # the fleet's own image by leaving this out.
            raise HTTPException(
                status_code=403,
                detail=(
                    "only the fleet's operator may choose the image an agent runs. "
                    "Leave `image` out to move onto the fleet's image."
                ),
            )
        store = store_factory()
        try:
            box = _operated_box(store, token, box_id)
            if background:
                return _upgrade_in_background(box, image)
            try:
                return upgrade_box(box.id, store=store, image=image)
            except UpgradeFailed as exc:
                # The substrate broke. 502, matching what `POST /api/boxes`
                # answers for the same class of failure — and *not* 409, which
                # would tell the caller they asked for the wrong thing when
                # flyctl was simply having a bad minute.
                raise HTTPException(status_code=502, detail=str(exc)) from exc
            except ProvisionError as exc:
                # 409: the caller asked for something this box cannot do right
                # now — terminal, mid-provision, or no image named. Every one of
                # them is actionable, which is what separates it from the above.
                raise HTTPException(status_code=409, detail=str(exc)) from exc
        finally:
            store.close()

    def _upgrade_in_background(box: Any, image: str | None) -> Any:
        """Refuse what is refusable, answer, then re-image on a thread.

        The refusals are checked here rather than in the thread because they
        are the caller's to fix: a `202` for a box that can never be upgraded
        is a lie the app would render as a spinner. `upgrade_box` checks them
        again — it is also the CLI's entry point — and doing it twice is the
        price of the early exit.
        """
        from fastapi.responses import JSONResponse

        from flotta.provision import _fleet_image

        if is_terminal("box", box.status):
            raise HTTPException(
                status_code=409,
                detail=f"box {box.id} is {box.status!r}; there is no machine left to re-image.",
            )
        if box.status == "provisioning":
            raise HTTPException(
                status_code=409,
                detail=(
                    f"box {box.id} is still provisioning. Upgrading mid-create would race "
                    "the thread building it; wait for it to settle."
                ),
            )
        target = (image or "").strip() or _fleet_image()
        if not target:
            raise HTTPException(
                status_code=409,
                detail=(
                    "no image to upgrade to. Pass one explicitly or set $FLOTTA_FLY_IMAGE "
                    "on the control plane."
                ),
            )

        def upgrade() -> None:
            from flotta.provision import upgrade_box

            inner = store_factory()
            try:
                upgrade_box(box.id, store=inner, image=target)
            except Exception as exc:  # noqa: BLE001
                # `upgrade_box` writes `upgrade_failed` before raising, so the
                # app learns about this from the timeline. This only stops a
                # stray exception ending the thread in silence.
                _log.warning("upgrading %s failed: %s: %s", box.name, type(exc).__name__, exc)
            finally:
                inner.close()

        # A thread, for the same reason `_create_in_background` uses one: this
        # endpoint is a sync `def`, so there is no running loop to schedule on,
        # and the work is blocking subprocess calls end to end.
        worker = threading.Thread(target=upgrade, name=f"upgrade-{box.name}", daemon=True)
        _provisioning.add(worker)
        worker.start()

        return JSONResponse(
            {"box_id": box.id, "box": _box_dict(box), "image": target, "started": True},
            status_code=202,
        )

    def confine_to_box(token: Token | None, box: Any, action: str) -> None:
        """Refuse a box token acting on any box but its own.

        Scopes say what a token may do, never to which box. A `box:` subject
        lives on a machine whose agent has root, so without this one box's
        token would reach every other box's grants. Any other subject — an
        operator — is unrestricted.
        """
        claimed = subject_box(token.subject) if token else None
        if claimed is not None and claimed not in {box.id, box.name}:
            raise HTTPException(
                status_code=403,
                detail=f"token for box {claimed!r} cannot {action} box {box.name!r}",
            )

    @app.get("/api/boxes/{box_id}/git-credential/repos")
    def git_credential_repos(box_id: str, token: Token | None = needs_git) -> Any:
        """The repositories this box may get a git credential for (FLOTTA-61).

        **For the agent, asked with its own token.** Asked which repositories
        it could reach, eng-r ran seven shell commands and three failed: the
        box has no fleet store and no signing key, correctly, and nothing told
        it the answer. `GET /repos` has it, but behind `fleet:read`, and a box
        holds only `git:credential`.

        So this is the same list under the scope the box already has, confined
        the same way minting is. Not `fleet:read` added to box tokens: that
        would let an agent read every box in the fleet to learn its own grants.
        Read-only, and names nothing the credential endpoint would not already
        refuse or allow — a box that can mint for a repository can learn that
        it can by trying.
        """
        store = store_factory()
        try:
            box = _scoped_box(store, token, box_id)
            confine_to_box(token, box, "list the repositories of")
            return {"box_id": box.id, "name": box.name, "repos": _usable_repos(store, box)}
        finally:
            store.close()

    async def _deliver(peer_name: str, text: str, *, title: str) -> str:
        """The default delivery: through the front door, as the app would.

        Mints its own short-lived `box:chat` token, which exists for seconds
        inside this process — never on a box, which is the entire point of
        relaying rather than handing one out.

        **It carries the peer's tenancy** (M11.6b), read from the same address
        the door will read: the door admits a token to its own org and client
        only, so a relay token without the claims reaches the default org's
        agents and no tenant's. With them, it reaches the one client the two
        agents share and nothing else.
        """
        if not key:
            raise HTTPException(
                status_code=503,
                detail=f"no ${SIGNING_KEY_ENV} configured on the control plane; "
                f"agents cannot message each other without one",
            )
        _, client, org = parse_address(peer_name)
        tenancy = (
            {} if (org, client) == (DEFAULT_ORG, DEFAULT_CLIENT) else {"org": org, "client": client}
        )
        token = mint(
            subject="relay",
            scopes={SCOPE_BOX_CHAT},
            ttl_s=int(RELAY_TIMEOUT_S) + 60,
            key=key,
            **tenancy,
        )
        return await deliver_through_door(
            peer_name, text, token=token, title=title, timeout_s=RELAY_TIMEOUT_S
        )

    carry = deliver if deliver is not None else _deliver

    @app.get("/api/boxes/{box_id}/peer/roster")
    def peer_roster(box_id: str, token: Token | None = needs_peer) -> Any:
        """Who this agent may message, asked with its own token.

        The twin of `git-credential/repos`, and for the same reason: an agent
        that cannot see the fleet cannot know who its colleagues are, and one
        that guesses a name spends a wake finding out it was wrong. Confined
        the same way — a box may ask about itself and no other.
        """
        store = store_factory()
        try:
            box = _scoped_box(store, token, box_id)
            confine_to_box(token, box, "list the colleagues of")
            return {
                "box_id": box.id,
                "name": box.name,
                "peers": _described(store, box, store.peers_for_box(box.id)),
            }
        finally:
            store.close()

    @app.post("/api/boxes/{box_id}/peer/ask")
    async def peer_ask(box_id: str, body: dict[str, Any], token: Token | None = needs_peer) -> Any:
        """Carry a message to another agent and bring back its reply (M7).

        The request is held open while the other agent thinks — it wakes,
        answers, and the reply comes back in this response. That is slow by
        nature and honest: the asking agent is blocked on a colleague exactly
        as a person would be, and its own tool deadline is longer than ours.

        **Every refusal names what to do instead.** An agent told only "403"
        will try again, differently, several times; one told "you were not
        granted eng-b, ask the person in the app" stops.
        """
        store = store_factory()
        hop = None
        try:
            box = _scoped_box(store, token, box_id)
            confine_to_box(token, box, "send messages as")

            wanted = str(body.get("peer") or "").strip()
            message = str(body.get("message") or "").strip()
            if not wanted:
                raise HTTPException(status_code=422, detail="which agent?")
            if not message:
                raise HTTPException(status_code=422, detail="what should they be told?")

            # Another client's agent is no agent: told "not ready" instead, the
            # asking agent would wait for one it may never reach.
            peer = _colleague(store, box, wanted)
            if peer is None:
                raise HTTPException(
                    status_code=404,
                    detail=f"there is no agent called {wanted!r} on this fleet. "
                    f"Your colleagues are listed by `flotta-ask --list`.",
                )
            if not store.may_message(box.id, peer.id):
                # Three reasons, three different things to do about them — an
                # agent told only "refused" tries again differently.
                if peer.id == box.id:
                    why, detail = (
                        "itself",
                        "that is you. Answer it yourself.",
                    )
                elif peer.id in store.blocked_peers_for_box(box.id):
                    why, detail = (
                        "blocked",
                        f"a person has stopped {box.name} from asking {peer.name!r}. They "
                        f"can allow it again in the Flotta app, under {box.name}'s Info "
                        f"panel, Colleagues. You cannot change it yourself.",
                    )
                else:
                    why, detail = (
                        "not ready",
                        f"{peer.name!r} is still being set up and cannot answer yet. "
                        f"Try again in a few minutes, or answer with what you have.",
                    )
                store.add_event(
                    "box",
                    box.id,
                    PEER_REFUSED,
                    {"peer": peer.name, "message": message[:EVENT_TEXT_CHARS], "reason": why},
                )
                raise HTTPException(status_code=403 if why == "blocked" else 409, detail=detail)

            # Every agent in a chain is a colleague of the last — one client.
            names = {b.id: b.name for b in store.list_boxes_in(box.org, box.client)}
            try:
                hop = chains.begin(box.id, peer.id, name=lambda i: names.get(i, i))
            except RelayRefused as exc:
                store.add_event(
                    "box",
                    box.id,
                    PEER_REFUSED,
                    {
                        "peer": peer.name,
                        "message": message[:EVENT_TEXT_CHARS],
                        "reason": str(exc)[:EVENT_TEXT_CHARS],
                    },
                )
                raise HTTPException(status_code=429, detail=str(exc)) from exc

            store.add_event(
                "box",
                box.id,
                PEER_ASKED,
                {"peer": peer.name, "message": message[:EVENT_TEXT_CHARS], "hop": hop.depth},
            )
            store.add_event(
                "box",
                peer.id,
                PEER_ASKED_BY,
                {"peer": box.name, "message": message[:EVENT_TEXT_CHARS], "hop": hop.depth},
            )

            started = time.monotonic()
            try:
                reply = await carry(
                    peer.name,
                    envelope(box.name, message),
                    title=f"{box.name} asked",
                )
            except HTTPException:
                raise
            except Exception as exc:
                seconds = round(time.monotonic() - started, 1)
                store.add_event(
                    "box",
                    box.id,
                    PEER_FAILED,
                    {"peer": peer.name, "reason": str(exc)[:EVENT_TEXT_CHARS], "seconds": seconds},
                )
                # 502, not 500: the failure is the other agent's or the path
                # to it, and the asking agent should say so rather than
                # reporting that Flotta is broken.
                raise HTTPException(
                    status_code=502,
                    detail=f"could not get an answer from {peer.name!r}: {exc}",
                ) from exc

            seconds = round(time.monotonic() - started, 1)
            store.add_event(
                "box",
                box.id,
                PEER_ANSWERED,
                {"peer": peer.name, "reply": reply[:EVENT_TEXT_CHARS], "seconds": seconds},
            )
            store.add_event(
                "box",
                peer.id,
                PEER_REPLIED,
                {"peer": box.name, "reply": reply[:EVENT_TEXT_CHARS], "seconds": seconds},
            )
            return {"peer": peer.name, "reply": reply, "seconds": seconds, "hop": hop.depth}
        finally:
            if hop is not None:
                chains.end(hop)
            store.close()

    @app.post("/api/boxes/{box_id}/git-credential")
    def git_credential(box_id: str, body: dict[str, Any], token: Token | None = needs_git) -> Any:
        """Mint a git credential for a repository this box is granted.

        Called by the box's git credential helper, which git invokes with the
        repository path (`credential.useHttpPath=true`). The box holds no
        GitHub credential of its own — that is the whole design: a token in the
        agent's environment is a token a prompt-injected agent can print, and
        we have watched this agent act on instructions that came from a
        repository.

        **What this does and does not enforce.** Flotta refuses to hand a
        credential for a repository the box was not granted. It does *not*
        constrain what the returned token can reach — the source is one fleet
        token, so a box that extracted it could use it beyond its grants. That
        is policy enforced here, not by GitHub, and closing it means minting
        GitHub App installation tokens scoped to `repository_ids`. Stated
        plainly because a soft boundary that reads as a hard one is worse than
        no boundary at all.

        **A box token may only ask about its own box.** Scopes say what a token
        may do, never to which box, and that gap became load-bearing the moment
        a `git:credential` token started living on a machine whose agent has
        root. Without this check one box's token would reach every other box's
        grants, which is precisely the containment the grants exist to provide.
        """
        source = (os.environ.get(GITHUB_TOKEN_ENV) or "").strip()
        if not source:
            raise HTTPException(
                status_code=503,
                detail=f"no ${GITHUB_TOKEN_ENV} configured on the control plane; "
                f"boxes can read public repositories and nothing else",
            )

        store = store_factory()
        try:
            box = _scoped_box(store, token, box_id)

            # Before anything else: is this token allowed to speak for this
            # box? A non-box subject (an operator) is unrestricted — see
            # `auth.BOX_SUBJECT_PREFIX`.
            confine_to_box(token, box, "mint credentials for")
            # Whatever the grants say: the credential is the operator's whole
            # token, and a grant from before FLOTTA-83 must not hand it over.
            if not reaches_fleet_github(box.org):
                raise HTTPException(status_code=403, detail=AGENCY_GITHUB_UNAVAILABLE)

            repo = str(body.get("repo") or "").strip()
            if not repo:
                raise HTTPException(status_code=422, detail="which repository?")
            try:
                if not store.may_use_repo(box.id, repo):
                    raise HTTPException(
                        status_code=403,
                        detail=f"box {box.name!r} is not granted {repo!r}. "
                        f"Grant it with `flotta repo grant {box.name} {repo}`.",
                    )
            except ValueError as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc

            store.add_event("box", box.id, "git_credential", {"repo": repo})
            # The username is ignored by GitHub when the password is a token;
            # `x-access-token` is the convention its own docs use.
            return {"username": "x-access-token", "password": source, "repo": repo}
        finally:
            store.close()

    @app.get("/api/boxes/{box_id}/address")
    def box_address(box_id: str, token: Token | None = needs_route) -> Any:
        """Which box this is and whose, without waking it (M11).

        For the front door, which has to know a box's tenant *before* it wakes
        it: the subject check sits between this and `wake`, so a token for
        another org is refused without a machine starting on its account.

        Deliberately the smallest answer that decides: no endpoint (an asleep
        box has none worth having), no tasks, no metadata. `GET /api/boxes/{id}`
        returns all of those and is one query per task.

        Guarded like `wake`, which it is the first half of: `fleet:route` for
        the door, which finds any org's box and then checks the caller's token
        itself, or `box:chat` for a person, who finds only their own tenant's.

        The tenancy is the box's own row (M11.4). Every box made before it
        belongs to the default org's default client, which is what a token
        with no `org` claim may reach.
        """
        store = store_factory()
        try:
            box = _routable_box(store, token, box_id)
            return {
                "box": {
                    "id": box.id,
                    "name": box.name,
                    "status": box.status,
                    "org": box.org,
                    "client": box.client,
                }
            }
        finally:
            store.close()

    @app.post("/api/boxes/{box_id}/wake")
    def wake_box_endpoint(box_id: str, token: Token | None = needs_route) -> Any:
        """Ensure a box is up so it can be addressed. Idempotent.

        Exists for the front door (M5b), which cannot reach the substrate
        itself: a request for `<box>.flotta.dev` normally arrives while the box
        is *asleep* — that is the cost argument, not an edge case — so
        something has to start it, and D10 says that something must be code
        that can reach the substrate. The door asks; the control plane acts.

        **Guarded by `box:chat`, not a scope of its own.** A box is asleep most
        of the time, so anything permitted to talk to one must be permitted to
        wake it or the permission means nothing. A separate `box:wake` would be
        a scope nobody could sensibly withhold. The door holds `fleet:route`
        instead, because it wakes any org's box once it has checked the caller
        (`_routable_box`).

        `wake_box`, not `start_box`: the operator's verb refuses anything that
        is not `stopped`, which is right when a human asks and wrong here — the
        addressing path has to accept an already-running box, and reconcile a
        row that disagrees with the substrate. Fly stops machines on its own
        during a host drain.
        """
        from flotta.provision import ProvisionError, wake_box

        store = store_factory()
        try:
            box = _routable_box(store, token, box_id)
            _record_activity(store, box.id)
            try:
                result = wake_box(box.id, store=store, reason="front-door")
            except ProvisionError as exc:
                # 409 for an illegal state (a torn-down box cannot be woken),
                # 502 for the substrate failing to start it. The caller can act
                # on the first and only retry the second.
                status = 409 if "only a running or stopped box" in str(exc) else 502
                raise HTTPException(status_code=status, detail=str(exc)) from exc
            return {"box": _box_dict(_routable_box(store, token, box.id)), **result}
        finally:
            store.close()

    @app.post("/api/boxes/{box_id}/sleep")
    def sleep_box_endpoint(box_id: str, token: Token | None = needs_write) -> Any:
        """Put an agent to sleep now, rather than when it has been idle long enough.

        The web app's "sleep" button (M12.8). The reconcile loop already sleeps
        an idle agent (`sleep_idle_boxes`); this is the same `stop_box`, asked
        for by a person, so the row moves only after the substrate released the
        machine. Idempotent: an agent already asleep answers 200.

        **`fleet:write`, not `box:chat`.** Waking takes `box:chat` because
        talking to an agent wakes it anyway. Sleeping is a decision about the
        agent, like changing its model, and a client's read-only view cannot
        make it.

        409 for what the caller can fix (a task still running, an agent that is
        not running), 502 for the substrate failing, as `/wake` answers.
        """
        from flotta.provision import ProvisionError, stop_box

        store = store_factory()
        try:
            box = _scoped_box(store, token, box_id)
            try:
                result = stop_box(box.id, store=store, reason="asked")
            except ProvisionError as exc:
                status = 502 if str(exc).startswith("could not stop") else 409
                raise HTTPException(status_code=status, detail=str(exc)) from exc
            return {"box": _box_dict(_scoped_box(store, token, box.id)), **result}
        finally:
            store.close()

    @app.post("/api/boxes/{box_id}/ticket")
    def chat_ticket(box_id: str, token: Token | None = needs_chat) -> Any:
        """A chat ticket for this one agent, for a browser to open it with (M12.5).

        For the web app's server, which holds the person's session and hands
        the ticket to the page for one connection: `box:chat` on this box and
        nothing else, for `TICKET_TTL_S`. The door takes it in
        `Sec-WebSocket-Protocol` from an allowed `Origin` (`flotta.door.app`);
        `protocols` is what the page offers and `url` where.

        Scoped like every box route, and by `admits` as well, so a box's own
        token cannot get a ticket for a colleague. Nothing is woken: the door
        does that when the socket opens, which may be never.
        """
        if key is None:
            raise HTTPException(
                status_code=503,
                detail=f"no {SIGNING_KEY_ENV}: a ticket is signed, and the door checks it",
            )
        store = store_factory()
        try:
            box = _scoped_box(store, token, box_id)
            if token is not None and admits(
                token, box_id=box.id, box_name=box.name, org=box.org, client=box.client
            ):
                raise HTTPException(status_code=404, detail=f"no box {box_id!r}")
            if is_terminal("box", box.status):
                raise HTTPException(status_code=409, detail=f"box {box.name!r} is {box.status}")
        finally:
            store.close()
        ticket = mint_ticket(
            subject=token.subject if token is not None else "operator",
            box_id=box.id,
            org=box.org,
            client=box.client,
            key=key,
        )
        from flotta.client import ticket_url

        return {
            "ticket": ticket,
            "ttl_s": TICKET_TTL_S,
            "box": {"id": box.id, "name": box.name},
            "url": ticket_url(box.name),
            "protocols": [CHAT_PROTOCOL, f"{TICKET_PROTOCOL_PREFIX}{ticket}"],
        }

    @app.delete("/api/boxes/{box_id}")
    def destroy_box(box_id: str, token: Token | None = needs_destroy) -> Any:
        """Destroy a box and everything it remembers. Idempotent.

        Goes through `teardown_box`, which cancels the backend's machine and
        fails any live tasks — writing `torn_down` straight into the store from
        here would close the row while the machine kept running and billing,
        the bug M0's review caught and M1 encoded a refusal for.
        """
        from flotta.provision import ProvisionError, teardown_box

        store = store_factory()
        try:
            box = _scoped_box(store, token, box_id)
            try:
                return {"result": teardown_box(box.id, store=store, reason="control-plane")}
            except (ProvisionError, UnknownEntityError) as exc:
                raise HTTPException(status_code=502, detail=str(exc)) from exc
        finally:
            store.close()

    return app


def _task_dict(task: Any) -> dict[str, Any]:
    from dataclasses import asdict

    return asdict(task)


def _event_dict(event: Any) -> dict[str, Any]:
    from dataclasses import asdict

    return asdict(event)


def serve(host: str = "127.0.0.1", port: int = 8080) -> None:  # pragma: no cover - entrypoint
    """Run the control plane. Refuses an unauthenticated public bind."""
    import uvicorn

    check_bind(host)
    described = (
        db.describe_url(os.environ.get(db.DATABASE_URL_ENV, ""))
        if os.environ.get(db.DATABASE_URL_ENV)
        else "a local SQLite file"
    )
    _log.info("control plane on %s:%s, fleet state in %s", host, port, described)
    # proxy_headers off: `caller_address` is the one reader of X-Forwarded-For.
    uvicorn.run(create_app(), host=host, port=port, proxy_headers=False)
