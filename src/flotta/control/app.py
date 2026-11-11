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
from datetime import UTC, datetime, timedelta
from typing import Any

from flotta import db
from flotta.address import parse as parse_address
from flotta.alerts import WebhookRefused, allow_local_webhooks, check_webhook_url, run_alert_loop
from flotta.approval_mail import run_approval_mail_loop
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
    SCOPE_ORG_GOVERN,
    SCOPE_RUN_CREATE,
    SIGNING_KEY_ENV,
    TICKET_PROTOCOL_PREFIX,
    TICKET_TTL_S,
    USER_SUBJECT_PREFIX,
    AuthError,
    Token,
    admits,
    admits_tenant,
    is_api_key,
    is_governor,
    key_token,
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
from flotta.github import GITHUB_TOKEN_ENV
from flotta.pulls import run_pull_request_loop
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
from flotta.runs import RUN_DEADLINE_S, chat_conversation
from flotta.store import (
    AGENCY_GITHUB_UNAVAILABLE,
    INVITE_TTL_S,
    LIMIT_KINDS,
    LINK_TTL_S,
    SEAT_ROLES,
    TASK_STATUSES,
    BoxBusyError,
    DuplicateMembershipError,
    DuplicateTenantError,
    FleetStore,
    InvalidAccountError,
    InvalidTenantError,
    InvalidTransitionError,
    LastOwnerError,
    LimitReachedError,
    NotOperatorError,
    OrgNotActiveError,
    UnknownEntityError,
    is_terminal,
    normalize_email,
    postgres_target,
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

#: How long `/machine`'s two slow reads took (FLOTTA-182). `server_logs` turns
#: it on at INFO beside the GitHub timing; the rest of flotta's INFO stays off.
MACHINE_TIMING_LOG = "flotta.control.machine"

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

#: Agents being resized by a thread of this process (FLOTTA-157), for the
#: reason `_model_changes_running` exists.
_resizes_running: set[str] = set()
_resizes_lock = threading.Lock()


def _record_unfinished_change(
    store_factory: Any, box_id: str, model: str, error: Exception
) -> None:
    """Record that a started model change ended early, on a connection of its own.

    The one it was using may be what failed. Best effort for the same reason:
    if this fails too, `MODEL_CHANGE_STALE_S` ends the change on the timeline.
    """
    from flotta.provision import MODEL_CHANGE_FAILED

    try:
        store = store_factory()
        try:
            store.add_event(
                "box", box_id, MODEL_CHANGE_FAILED, {"model": model, "error": str(error)[:400]}
            )
        finally:
            store.close()
    except Exception as exc:  # noqa: BLE001
        _log.warning("could not record %s's unfinished model change: %s", box_id, exc)


def _record_unfinished_resize(
    store_factory: Any, box_id: str, memory_mb: int, error: Exception
) -> None:
    """`_record_unfinished_change`, for a resize: on its own connection, best effort."""
    from flotta.provision import SIZE_CHANGE_FAILED

    try:
        store = store_factory()
        try:
            store.add_event(
                "box",
                box_id,
                SIZE_CHANGE_FAILED,
                {"memory_mb": memory_mb, "error": str(error)[:400]},
            )
        finally:
            store.close()
    except Exception as exc:  # noqa: BLE001
        _log.warning("could not record %s's unfinished resize: %s", box_id, exc)


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
#: Calls one webhook trigger may take per minute, counted once a call has
#: proved it holds the secret: a stranger's calls cannot use up a client's
#: allowance. Above the queue cap's three waiting runs on purpose; this is a
#: guard on a caller in a loop, the cap is the guard on cost.
HOOK_LIMIT_PER_TRIGGER = 30
HOOK_LIMIT_WINDOW_S = 60.0
#: A chat run's prompt when the turn began without one the door heard: the
#: agent carrying on by itself, or a prompt sent before the door was listening.
CHAT_NO_PROMPT = "(a turn the door heard begin without a prompt)"
#: The most steps the door may send for one turn; the store keeps `MAX_STEPS`.
CHAT_MAX_STEPS_SENT = 2_000
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


#: The Postgres stores this process has prepared (schema laid, migrations
#: applied), by URL. `_store_factory` prepares each once.
_prepared: set[str] = set()


def _store_factory() -> FleetStore:
    """A fresh store per request, **prepared once per process**, on a reused
    connection.

    Per-request rather than one long-lived store, for the same reason the
    dashboard opens SQLite per request: a pinned snapshot served to a polling
    UI is stale in the one way a fleet view must never be. Reusing the
    *connection* underneath does not change that; each store still reads the
    database as it is now.

    **Preparing, on Postgres, takes a lock.** Opening a store lays the schema
    and applies migrations under a fleet-wide advisory lock, so doing it per
    request queued every request behind every other. The first open in this
    process prepares, and every later one only connects. A deploy is a new
    process, so new migrations still apply on its first request.

    **And connecting is not cheap either** (FLOTTA-178). Removing the lock
    changed nothing measurable on Render: a signed-in request opens two stores
    (the session, then the route), and opening a Postgres connection was 82%
    of the control plane's CPU, more when a page's dozen requests connect at
    once. On half a CPU that is a second and a half per page. So the stores
    here reuse idle connections (`db.connect`'s `reuse`), which took a page's
    burst from 391 ms to 18 ms on a server held to half a CPU.

    SQLite is prepared on every open, as before: it is a local file, which can
    be deleted under a running server, and preparing it costs nothing. It has
    no connections worth keeping, either.

    Two first requests at once may both prepare, which is what every request
    did before and is safe for the same reason (the lock).
    """
    # No argument on purpose: FleetStore owns the $FLOTTA_DATABASE_URL /
    # $FLOTTA_STORE / ./fleet.db chain. This function used to re-implement it
    # and got it wrong — the service honoured the database URL but not
    # $FLOTTA_STORE, and served an empty fleet from a file nobody meant while
    # `flotta ps` in the same shell listed the real one.
    url = postgres_target()
    if url is None:
        return FleetStore()
    store = FleetStore(prepare=url not in _prepared, reuse=True)
    _prepared.add(url)
    return store


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
    clients_answer_approvals: bool = False,
    memory_mb: int | None = None,
    size_change: dict[str, Any] | None = None,
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
    out["clients_answer_approvals"] = clients_answer_approvals
    # The machine's memory in MB (FLOTTA-157), from the store rather than the
    # substrate (`provision.box_sizes`); None when nothing has said. And a
    # resize under way, which restarts the agent the way a model change does.
    out["memory_mb"] = memory_mb
    out["size_change"] = size_change
    return out


def _with_meta(store: Any, box: Any) -> dict[str, Any]:
    from flotta.provision import (
        box_sizes,
        fleet_model,
        model_changes,
        model_given_by_default,
        size_changes,
    )

    return _box_dict(
        box,
        store.meta_for_box(box.id),
        model=store.model_for_box(box.id),
        fleet_model=fleet_model(store),
        given=store.models_given([box.id]).get(box.id) or model_given_by_default(),
        model_change=model_changes(store, [box.id]).get(box.id),
        clients_answer_approvals=store.clients_answer_approvals(box.id),
        memory_mb=box_sizes(store, [box.id]).get(box.id),
        size_change=size_changes(store, [box.id]).get(box.id),
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
    #: Starts the first fleet-image lookup when the server starts, so the first
    #: Settings tab after a deploy does not wait on `flyctl releases`
    #: (FLOTTA-182). `None` warms nothing — the suite's default, because the
    #: real one runs `flyctl` whenever `$FLOTTA_FLY_APP` is in the environment
    #: a test inherited. The servers pass `provision.warm_release_cache`.
    warm_release_cache: Any = None,
    signing_key: str | None = None,
    #: Asks GitHub whether the fleet token can reach a repository, before a
    #: grant is stored. Injected for the same reason every Fly touchpoint is:
    #: the suite must stay hermetic and $0, and the default here would make a
    #: real HTTPS call the moment `$FLOTTA_GITHUB_TOKEN` was set in the
    #: environment a test happened to inherit.
    reachable: Any = None,
    #: The GitHub App agencies connect their own GitHub through (FLOTTA-22).
    #: Injected for the reason `reachable` is. `None` reads the environment,
    #: which may say this deployment has not registered one.
    github_app: Any = None,
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
    #: Reaches MCP servers' OAuth endpoints (FLOTTA-125), injected for the reason
    #: `reachable` is. An `httpx.BaseTransport`; `None` is the network.
    mcp_oauth_transport: Any = None,
    #: Holds one run's conversation with its agent (`flotta.runner.Deliver`).
    #: Injected for the reason `deliver` is. `None` goes through the front
    #: door, and needs a signing key to mint the run's token.
    deliver_run: Any = None,
    #: Reads and merges the pull requests agents open (FLOTTA-171), as
    #: `pulls.Fetch`. Injected for the reason `reachable` is; `None` is GitHub.
    github_fetch: Any = None,
    #: Judges a request before its agent starts (FLOTTA-173): `judge.judge`'s
    #: signature. Injected for the reason `reachable` is; `None` asks Jev.
    judge_request: Any = None,
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
    from fastapi import Depends, FastAPI, Header, HTTPException, Query

    from flotta.mail import (
        DECLINE_REASON_MAX,
        Message,
        approved_message,
        declined_message,
        invite_message,
        print_mailer,
        resolve_app_url,
        resolve_mailer,
        resolve_reply_to,
        resumed_message,
        sign_in_message,
        sign_in_url,
        sign_up_message,
        suspended_message,
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
    reply_to = resolve_reply_to()
    local_webhooks = allow_local_webhooks(keyless=not key)
    # Where tickets send a browser: refused now, not in a ticket nobody can use.
    from flotta.client import browser_door

    browser_door()
    per_address = RateLimit(LINK_LIMIT_PER_ADDRESS, LINK_LIMIT_WINDOW_S)
    per_ip = RateLimit(LINK_LIMIT_PER_IP, LINK_LIMIT_WINDOW_S)
    invites_per_org = RateLimit(INVITE_LIMIT_PER_ORG, LINK_LIMIT_WINDOW_S)
    invites_per_pending_org = RateLimit(INVITE_LIMIT_PER_PENDING_ORG, LINK_LIMIT_WINDOW_S)
    hooks_per_trigger = RateLimit(HOOK_LIMIT_PER_TRIGGER, HOOK_LIMIT_WINDOW_S)
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

        **So is an API key** (M14.5), for the same reason: a row, not a
        signature. It becomes a token for its one agent holding `run:create`
        alone (`_api_key_token`), which only the run routes accept.
        """

        def dependency(
            request: Request, authorization: str | None = Header(default=None)
        ) -> Token | None:
            scheme, _, credential = (authorization or "").partition(" ")
            if scheme.lower() == "session":
                token = _session_token(credential.strip(), request)
            elif scheme.lower() == "bearer" and is_api_key(credential.strip()):
                token = _api_key_token(credential.strip())
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

    def _api_key_token(secret: str) -> Token:
        """The token a live API key is, or 401.

        Looked up on every request, so a revoked key, or one whose agent was
        destroyed, is refused on its next one. The tenancy is the box's, read
        now, not what the key was made with.
        """
        store = store_factory()
        try:
            found = store.api_key(secret)
            box = store.get_box(found.box_id) if found is not None else None
        finally:
            store.close()
        if found is None or box is None:
            raise HTTPException(
                status_code=401,
                detail="no such API key, or it has been revoked",
                headers={"WWW-Authenticate": "Bearer"},
            )
        now = int(time.time())
        return key_token(
            key_id=found.id,
            org=box.org,
            client=box.client,
            box_id=box.id,
            issued_at=int(datetime.fromisoformat(found.created_at).timestamp()),
            # The stand-in token's, for this one request: a key does not
            # expire, and revoking it is what stops it (`docs/API_KEYS.md`).
            expires_at=now + 60,
        )

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
    #: An org's limits (FLOTTA-183): read by its own people, the operator or
    #: the hosted product's billing (`org:govern`); set by the last two.
    #: What the org's own people read, and the hosted product's billing too:
    #: an org's limits (FLOTTA-183) and its usage totals (FLOTTA-188). One rule,
    #: so the two never drift apart. Each route still checks whose org it is.
    needs_read_or_govern = Depends(require(SCOPE_FLEET_READ, SCOPE_ORG_GOVERN, any_of=True))
    needs_limits_write = Depends(require(SCOPE_ORG_ADMIN, SCOPE_ORG_GOVERN, any_of=True))
    #: The agency's people, or a client's person who talks to agents: who may
    #: answer a run's approval, before the rules of `answer_run_approval`.
    needs_answer = Depends(require(SCOPE_FLEET_WRITE, SCOPE_BOX_CHAT, any_of=True))
    needs_chat = Depends(require(SCOPE_BOX_CHAT))
    # Any valid credential at all: for the routes about the caller themselves.
    needs_anyone = Depends(require())
    # The door's `fleet:route`, or a person's `box:chat` — see `_routable_box`.
    needs_route = Depends(require(SCOPE_FLEET_ROUTE, SCOPE_BOX_CHAT, any_of=True))
    needs_fleet_route = Depends(require(SCOPE_FLEET_ROUTE))
    needs_route_or_write = Depends(require(SCOPE_FLEET_ROUTE, SCOPE_FLEET_WRITE, any_of=True))
    # The agency, or an API key (M14.5): the run routes a key may use, and
    # only those. A key's token is confined to its box (`key_token`).
    needs_run_write = Depends(require(SCOPE_FLEET_WRITE, SCOPE_RUN_CREATE, any_of=True))
    needs_run_read = Depends(require(SCOPE_FLEET_READ, SCOPE_RUN_CREATE, any_of=True))
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
        if is_governor(token):
            # The hosted product's billing credential is the default org's and
            # unconfined, so it looks like the operator's. It is not one: it
            # may read and set orgs' limits (FLOTTA-183) and suspend and resume
            # them (FLOTTA-184), and nothing else.
            return False
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

    def _require_owner(token: Token | None, action: str) -> None:
        """403 unless an org's owner (`org:admin`) or the operator.

        For what changes what an agency pays rather than what its agents do — a
        machine's size (FLOTTA-157). A member may restart, rename and re-model
        an agent, and the bill is still the owner's to choose. The route's own
        dependency asks for `fleet:write` as well, so the operator viewing an
        agency as its owner, which holds `org:admin` and no write, is refused
        there before this.
        """
        if token is None or _is_operator(token) or token.allows(SCOPE_ORG_ADMIN):
            return
        raise HTTPException(status_code=403, detail=f"only the org's owner may {action}")

    def _checked_memory(raw: Any) -> int:
        """A body's `memory_mb`, on the fleet's allowlist, or 422 saying which are."""
        from flotta.backend import check_memory_mb

        try:
            return check_memory_mb(raw)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    def _scoped_box(store: Any, token: Token | None, ref: str) -> Any:
        """A box by id or name that `token` may act on, or 404.

        Another org's box is the same 404 as no box: "exists, but not yours"
        would make every route a directory of other people's agents.
        """
        box = store.get_box(ref) or store.get_box_by_name(ref)
        if box is None or (
            token is not None
            and (
                admits_tenant(token, org=box.org, client=box.client) is not None
                # A run's token names its own box (`runner.run_token`).
                or (token.box is not None and token.box != box.id)
            )
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

    def _github_fetch(method: str, url: str, **kwargs: Any) -> tuple[int, Any]:
        if github_fetch is not None:
            return github_fetch(method, url, **kwargs)
        from flotta.github_app import _httpx_fetch

        return _httpx_fetch(method, url, **kwargs)

    def _sweep_token(store: Any, box: Any, repo: str) -> str:
        """A repository token for the pull-request sweep, refused as the box would be."""
        from flotta.repo_tokens import repo_token

        return repo_token(
            store,
            box,
            repo,
            github_app=_github_app_or_none(),
            fleet_token=(os.environ.get(GITHUB_TOKEN_ENV) or "").strip(),
        )[0]

    @asynccontextmanager
    async def lifespan(app: Any):
        task: asyncio.Task | None = None
        alerts: asyncio.Task | None = None
        notices: asyncio.Task | None = None
        runs: asyncio.Task | None = None
        pulls_task: asyncio.Task | None = None
        if warm_release_cache is not None:
            # On its own thread: startup does not wait for it, and a lookup
            # that fails costs the first panel a wait, which is what it cost
            # before.
            with contextlib.suppress(Exception):
                warm_release_cache()
        if run_loop:
            # `loop_runner` makes the *interesting* failure testable: a loop
            # that is configured and supposed to be running, but is not
            # sweeping. That is what a slept platform looks like from inside
            # the process, and it is the case `/health` exists to catch — a
            # real loop cannot test it, because a real loop sweeps.
            runner = loop_runner or run_reconcile_loop
            task = asyncio.create_task(runner(state, store_factory=store_factory))
            # Its own task, so that a webhook can never hold up a sweep.
            alerts = asyncio.create_task(
                run_alert_loop(
                    store_factory,
                    mailer=deliver_link,
                    app_url=link_base,
                    signing_key=key,
                    allow_local=local_webhooks,
                )
            )
            # Approvals waiting past a minute, emailed to whoever can answer (FLOTTA-167).
            notices = asyncio.create_task(
                run_approval_mail_loop(store_factory, mailer=deliver_link, app_url=link_base)
            )
            # Pull requests agents opened, read again from GitHub (FLOTTA-171/172).
            pulls_task = asyncio.create_task(
                run_pull_request_loop(store_factory, token_for=_sweep_token, fetch=_github_fetch)
            )
            if carry_run is not None:
                from flotta.runner import run_run_loop

                runs = asyncio.create_task(run_run_loop(store_factory, carry_run))
            else:
                _log.warning("no signing key: runs wait in pending, since none can be delivered")
        try:
            yield
        finally:
            if runs is not None:
                runs.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await runs
            if task is not None:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
            if alerts is not None:
                alerts.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await alerts
            if notices is not None:
                notices.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await notices
            if pulls_task is not None:
                pulls_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await pulls_task

    app = FastAPI(title="Flotta control plane", lifespan=lifespan)
    app.state.loop_state = state

    @app.exception_handler(LimitReachedError)
    def _limit_reached(_request: Request, exc: LimitReachedError) -> Any:
        """402 wherever an org's limit refuses a create (FLOTTA-183).

        One handler rather than a catch in every route, because the store is
        where the limit is held and any route that reaches it must answer the
        same way: a route added later that creates a client, a seat or an
        agent cannot forget to. 402, like a budget's refusal: what is asked for
        is more than the org's plan has, and raising the limit is the fix.
        """
        from fastapi.responses import JSONResponse

        return JSONResponse(
            status_code=402,
            content={"detail": str(exc), "limit": {"kind": exc.kind, "limit": exc.limit}},
        )

    @app.exception_handler(OrgNotActiveError)
    def _org_not_active(_request: Request, exc: OrgNotActiveError) -> Any:
        """403 wherever an org that is not active is refused a create.

        The routes that met it before (agents, invites) caught it themselves.
        A suspended org is refused in more places (FLOTTA-184: clients, people,
        an invite's redeem), and the store is where it is held, so one handler
        answers for every route that reaches it, as for an org's limits.
        """
        from fastapi.responses import JSONResponse

        return JSONResponse(status_code=403, content={"detail": str(exc)})

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

    # -- limits (FLOTTA-183, M15.1) -------------------------------------------
    #
    # How many clients, seats and agents an org may have. Held in the store, so
    # every path that creates one meets it; set here by the operator, or by the
    # hosted product's billing from the org's plan with an `org:govern` token,
    # which can do this and nothing else (`control/test_govern.py`).

    def _limits_view(store: Any, org: Any) -> dict[str, Any]:
        counts = store.org_counts(org.id)
        limits = {kind: org.limit(kind) for kind in LIMIT_KINDS}
        return {
            "org": org.id,
            "limits": limits,
            "counts": counts,
            # Over, not at: a limit lowered below what the org already holds.
            # Nothing is taken away; the next one is refused.
            "over": {
                kind: limits[kind] is not None and counts[kind] > limits[kind]
                for kind in LIMIT_KINDS
            },
        }

    @app.get("/api/orgs/{org_id}/limits")
    def get_org_limits(org_id: str, token: Token | None = needs_read_or_govern) -> Any:
        """An org's limits and what it holds of each: `{limits, counts, over}`.

        For the org's own people (not a client's: a client sees its agents,
        not its agency's plan), the operator, and `org:govern`. Anyone else's
        org is the 404 every tenant route gives.
        """
        if not (_is_operator(token) or (token is not None and is_governor(token))):
            assert token is not None
            if token.client is not None or (token.org or DEFAULT_ORG) != org_id:
                raise HTTPException(status_code=404, detail=f"no org with id {org_id!r}")
        store = store_factory()
        try:
            return _limits_view(store, _org_or_404(store, org_id))
        finally:
            store.close()

    @app.put("/api/orgs/{org_id}/limits")
    def put_org_limits(
        org_id: str, body: dict[str, Any], token: Token | None = needs_limits_write
    ) -> Any:
        """Set an org's limits: `{"clients": 5, "seats": 1, "agents": null}`.

        A limit left out is kept and `null` is none. Below what the org holds is
        allowed: it refuses the next one and removes nothing. The operator's,
        or the hosted product's billing (`org:govern`); not an org's own owner,
        whose limits are its plan's.
        """
        if not (_is_operator(token) or (token is not None and is_governor(token))):
            raise HTTPException(
                status_code=403,
                detail="only the fleet's operator, or its billing, may set an org's limits",
            )
        store = store_factory()
        try:
            _org_or_404(store, org_id)
            try:
                org = store.set_org_limits(org_id, dict(body))
            except InvalidTenantError as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc
            _log.info(
                "limits of %s set by %s: %s",
                org_id,
                token.subject if token is not None else "the operator",
                {kind: org.limit(kind) for kind in LIMIT_KINDS},
            )
            return _limits_view(store, org)
        finally:
            store.close()

    @app.post("/api/orgs/{org_id}/approve")
    def approve_org(
        org_id: str,
        tasks: BackgroundTasks,
        body: dict[str, Any] | None = None,
        token: Token | None = needs_limits_write,
    ) -> Any:
        """Let a signed-up org create agents. Final.

        The operator's, or the hosted product's billing (`org:govern`) once an
        agency's first invoice is paid (FLOTTA-185). **Billing's approval always
        comes with a starting cap**: an org-wide model budget of
        `FLOTTA_STARTING_CAP_EUR` a month that pauses at the limit, until billing
        lifts it at the first renewal (`DELETE …/starting-cap`). The operator's
        comes without one unless asked, with `{"starting_cap": true}`.
        """
        from flotta.settings import starting_cap_eur_nano

        governed = token is not None and is_governor(token)
        if not (governed or _is_operator(token)):
            raise HTTPException(
                status_code=403,
                detail="only the fleet's operator, or its billing, may approve an org",
            )
        asked = (body or {}).get("starting_cap", False)
        if not isinstance(asked, bool):
            raise HTTPException(status_code=422, detail="starting_cap is true or false")
        cap: int | None = None
        if governed or asked:
            store = store_factory()
            try:
                cap = starting_cap_eur_nano(store)
            finally:
                store.close()
        by = token.subject if token is not None else "operator"
        return _decide(org_id, tasks, approve=True, starting_cap_eur_nano=cap, by=by)

    @app.delete("/api/orgs/{org_id}/starting-cap")
    def lift_starting_cap(org_id: str, token: Token | None = needs_limits_write) -> Any:
        """Lift the starting cap a self-serve approval gave an org (FLOTTA-185).

        Billing's, once a renewal invoice is paid, or the operator's; never the
        org's own, whose cap it is. Every budget the org set itself stays.
        `{"lifted": false}` when there is none, so a webhook delivered twice is
        harmless.
        """
        if not (_is_operator(token) or (token is not None and is_governor(token))):
            raise HTTPException(
                status_code=403,
                detail="only the fleet's operator, or its billing, may lift a starting cap",
            )
        by = token.subject if token is not None else "operator"
        store = store_factory()
        try:
            _org_or_404(store, org_id)
            lifted = store.lift_starting_cap(org_id, by=by)
        finally:
            store.close()
        if lifted:
            _log.info("the starting cap of %s was lifted by %s", org_id, by)
        return {"org": org_id, "lifted": lifted}

    @app.post("/api/orgs/{org_id}/decline")
    def decline_org(
        org_id: str,
        tasks: BackgroundTasks,
        body: dict[str, Any] | None = None,
        token: Token | None = needs_admin,
    ) -> Any:
        """Refuse a signed-up org. It keeps its sign-in and creates nothing, ever.

        `{"reason": "..."}` is optional: the operator's words, sent to its owners.
        """
        _require_operator(token, "decline an org")
        reason = str((body or {}).get("reason") or "").strip()
        if len(reason) > DECLINE_REASON_MAX:
            raise HTTPException(
                status_code=422,
                detail=f"a reason has at most {DECLINE_REASON_MAX} characters",
            )
        return _decide(org_id, tasks, approve=False, reason=reason or None)

    def _decide(
        org_id: str,
        tasks: BackgroundTasks,
        *,
        approve: bool,
        reason: str | None = None,
        starting_cap_eur_nano: int | None = None,
        by: str | None = None,
    ) -> Any:
        """Decide, then tell the org's owners (FLOTTA-143).

        The decision is final once written, and the messages go after the
        response, like a sign-in link's: a slow mail provider must not make an
        answer that was saved look like one that failed, whose retry is a 409.
        `told` is how many owners a message was queued for, and `null` when
        messages are printed to the log rather than emailed. The reason is kept
        on the org, so a message that never arrives is not its only copy.
        """
        from dataclasses import asdict

        store = store_factory()
        try:
            org = store.decide_org(
                org_id,
                approve=approve,
                reason=reason,
                starting_cap_eur_nano=starting_cap_eur_nano,
                by=by,
            )
            owners = _owners(store, org_id)
            cap = store.starting_cap(org_id) if approve else None
        except UnknownEntityError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except InvalidTransitionError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        finally:
            store.close()
        told = _tell_owners(
            tasks,
            org_id,
            [
                approved_message(email, link_base, org.name, reply_to=reply_to)
                if approve
                else declined_message(email, link_base, org.name, reason, reply_to=reply_to)
                for email in owners
            ],
        )
        answer: dict[str, Any] = {"org": asdict(org), "told": told}
        if approve:
            from flotta.budgets import budget_dict

            answer["starting_cap"] = budget_dict(cap) if cap is not None else None
        return answer

    def _owners(store: Any, org_id: str) -> list[str]:
        """The addresses of an org's owners whose accounts are not disabled."""
        return [
            user.email
            for m in store.members_of(org_id)
            if m.role == "owner"
            and (user := store.get_user(m.user_id)) is not None
            and user.disabled_at is None
        ]

    def _tell_owners(tasks: BackgroundTasks, org_id: str, messages: list[Message]) -> int | None:
        """Queue each owner's message for after the response; how many, as `told`.

        `null` when messages are printed to the log rather than emailed, and 0
        when there is no way to send them.
        """
        if deliver_link is None:
            return 0
        for message in messages:
            tasks.add_task(_tell_owner, message, org_id)
        return None if deliver_link is print_mailer else len(messages)

    def _tell_owner(message: Message, org_id: str) -> None:
        """One owner's message, after the response. A failure is logged, never raised."""
        assert deliver_link is not None
        try:
            deliver_link(message)
        except Exception:  # noqa: BLE001 - the change stands; the log says who was not told
            _log.exception("could not tell %s about %s (%s)", message.to, org_id, message.kind)

    # -- suspending an org (FLOTTA-184, M15.2) --------------------------------
    #
    # How a final failed payment ends: the hosted product's billing suspends an
    # org once its payment's retries give up, and resumes it when it pays. The
    # store holds the rule; these say who may, and tell the owners.

    @app.post("/api/orgs/{org_id}/suspend")
    def suspend_org(
        org_id: str,
        tasks: BackgroundTasks,
        body: dict[str, Any] | None = None,
        token: Token | None = needs_limits_write,
    ) -> Any:
        """Suspend an active org: nothing of it runs or is created until resumed.

        `{"reason": "..."}` is optional, kept on the org and sent to its owners.
        Its running agents are put to sleep after the response. Suspending one
        already suspended answers `changed: false` and tells nobody again, so a
        billing webhook delivered twice is harmless. The operator's, or billing's.
        """
        reason = str((body or {}).get("reason") or "").strip()
        if len(reason) > DECLINE_REASON_MAX:
            raise HTTPException(
                status_code=422,
                detail=f"a reason has at most {DECLINE_REASON_MAX} characters",
            )
        return _set_suspended(org_id, tasks, token, suspend=True, reason=reason or None)

    @app.post("/api/orgs/{org_id}/resume")
    def resume_org(
        org_id: str, tasks: BackgroundTasks, token: Token | None = needs_limits_write
    ) -> Any:
        """Resume a suspended org. Wakes nothing: its agents wake when next addressed."""
        return _set_suspended(org_id, tasks, token, suspend=False)

    def _set_suspended(
        org_id: str,
        tasks: BackgroundTasks,
        token: Token | None,
        *,
        suspend: bool,
        reason: str | None = None,
    ) -> Any:
        from dataclasses import asdict

        verb = "suspend" if suspend else "resume"
        if not (_is_operator(token) or (token is not None and is_governor(token))):
            raise HTTPException(
                status_code=403,
                detail=f"only the fleet's operator, or its billing, may {verb} an org",
            )
        by = token.subject if token is not None else "operator"
        store = store_factory()
        try:
            if suspend:
                org, changed = store.suspend_org(org_id, reason=reason, by=by)
            else:
                org, changed = store.resume_org(org_id, by=by)
            owners = _owners(store, org_id) if changed else []
        except UnknownEntityError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except InvalidTransitionError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        finally:
            store.close()
        told: int | None = 0
        if changed:
            _log.info("%s %sed by %s%s", org_id, verb, by, f": {reason}" if reason else "")
            if suspend:
                tasks.add_task(_sleep_suspended, org_id)
            told = _tell_owners(
                tasks,
                org_id,
                [
                    suspended_message(email, link_base, org.name, reason, reply_to=reply_to)
                    if suspend
                    else resumed_message(email, link_base, org.name, reply_to=reply_to)
                    for email in owners
                ],
            )
        return {"org": asdict(org), "changed": changed, "told": told}

    def _sleep_suspended(org_id: str) -> None:
        """After a suspend's response: its running agents to sleep now. The
        reconcile loop does the same each sweep, for anything this misses."""
        from flotta.provision import sleep_suspended_orgs

        store = store_factory()
        try:
            sleep_suspended_orgs(store, org_id=org_id)
        except Exception:  # noqa: BLE001 - the loop tries again next sweep
            _log.exception("could not put %s's agents to sleep after suspending it", org_id)
        finally:
            store.close()

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

    # -- an agency's own GitHub (FLOTTA-22) ----------------------------------
    #
    # Three steps, because GitHub's redirect proves nothing by itself. `connect`
    # sends the person to GitHub's install screen. GitHub sends them back to the
    # web app, whose cookie says who they are, with an installation id anyone
    # could have typed. `authorize` sends them to GitHub again, to say who they
    # are; `claim` checks GitHub's answer, and only then stores anything. See
    # `flotta.github_app`.

    def _github_app_or_none() -> Any:
        if github_app is not None:
            return github_app
        from flotta.github_app import app_from_env

        return app_from_env(os.environ)

    def _configured_github_app() -> Any:
        """The App, or 503 naming the settings this deployment still needs."""
        app_ = _github_app_or_none()
        if app_ is None:
            from flotta.github_app import missing_settings

            raise HTTPException(
                status_code=503,
                detail="this deployment has no GitHub App; it still needs "
                + ", ".join(f"${name}" for name in missing_settings(os.environ)),
            )
        return app_

    def _repo_token(store: Any, box: Any, repo: str) -> tuple[str, str, str]:
        """`repo_tokens.repo_token` for a route: its refusal is the route's answer."""
        from flotta.repo_tokens import RepoTokenError, repo_token

        try:
            return repo_token(
                store,
                box,
                repo,
                github_app=_github_app_or_none(),
                fleet_token=(os.environ.get(GITHUB_TOKEN_ENV) or "").strip(),
            )
        except RepoTokenError as exc:
            raise HTTPException(status_code=exc.status, detail=exc.detail) from exc

    def _state_key() -> str:
        """Signing a state needs the fleet's key, and an open control plane has none."""
        if not key:
            raise HTTPException(
                status_code=503, detail="connecting GitHub needs $FLOTTA_SIGNING_KEY set"
            )
        return key

    def _started_by_caller(token: Token | None, raw: Any) -> Any:
        """The state of a connection this caller started, for an org they administer.

        **The same person, not just the same org.** A state links an installation
        to a tenant, so a link one person started and another finished is how a
        GitHub admin gets tricked into giving an agency their account.
        """
        from flotta.github_app import BadState, read_state

        try:
            state = read_state(_state_key(), str(raw or ""))
        except BadState as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        if token is None or token.subject != state.subject:
            raise HTTPException(
                status_code=403,
                detail="this connection was started by someone else; start again yourself",
            )
        _admin_of(token, state.org)
        return state

    def _connection(installation: Any, app_: Any) -> dict[str, Any]:
        """One connection as the agency sees it, with GitHub's word on its state."""
        from flotta.github_app import GitHubAppError, InstallationGone

        status = "unknown"
        if app_ is not None:
            try:
                suspended = app_.account(installation.installation_id).suspended
                status = "suspended" if suspended else "active"
            except InstallationGone:
                status = "removed"
            except GitHubAppError:
                pass
        where = (
            f"organizations/{installation.account}/"
            if installation.account_type == "Organization"
            else ""
        )
        return {
            "installation_id": installation.installation_id,
            "account": installation.account,
            "client": installation.client,
            "connected_at": installation.connected_at,
            # Whom this account's agents commit as (FLOTTA-105). None for an
            # account connected before migration 9: its agents commit as
            # themselves until somebody connects it again.
            "commits_as": installation.connected_login,
            "status": status,
            "manage_url": (
                f"https://github.com/{where}settings/installations/{installation.installation_id}"
            ),
        }

    @app.get("/api/orgs/{org_id}/github")
    def list_github(org_id: str, token: Token | None = needs_admin) -> Any:
        """The GitHub accounts this org has connected, and whether it can connect one."""
        _admin_of(token, org_id)
        store = store_factory()
        try:
            _org_or_404(store, org_id)
            installations = store.github_installations(org_id)
        finally:
            store.close()
        app_ = _github_app_or_none()
        return {
            "available": app_ is not None,
            "connections": [_connection(i, app_) for i in installations],
        }

    @app.post("/api/orgs/{org_id}/github/connect")
    def connect_github(org_id: str, body: dict[str, Any], token: Token | None = needs_admin) -> Any:
        """Where to send a person to connect GitHub: `{"client": "acme"}`, or the whole org.

        Nothing is stored until `claim`.
        """
        from flotta.github_app import State, sign_state

        _admin_of(token, org_id)
        client = str(body.get("client") or "").strip() or None
        store = store_factory()
        try:
            _org_or_404(store, org_id)
            if client is not None and store.get_client(org_id, client) is None:
                raise HTTPException(status_code=404, detail=f"no client {client!r}")
        finally:
            store.close()
        app_ = _configured_github_app()
        signing = _state_key()
        assert token is not None
        state = State(org=org_id, client=client, subject=token.subject)
        return {"url": app_.install_url(sign_state(signing, state))}

    @app.post("/api/orgs/{org_id}/github/{installation_id}/reconnect")
    def reconnect_github(
        org_id: str, installation_id: str, token: Token | None = needs_admin
    ) -> Any:
        """Where to send a person to prove, again, that they own a connected account.

        An account the App is already installed on never reaches `authorize`
        by way of `connect`: GitHub answers the install screen with the
        installation's own settings page and drops the state, and an update
        made there carries none either. So this starts at the step after it.
        The state names the installation and the scope it is held under, and
        whoever finishes it is the one GitHub then vouches for: the person
        the account's agents commit as (FLOTTA-105), for a connection from
        before that was kept, or for a new person taking it over.

        Nothing changes until `claim`, as with `connect`.
        """
        from flotta.github_app import State, sign_state

        _admin_of(token, org_id)
        store = store_factory()
        try:
            installation = store.github_installation(org_id, installation_id)
        finally:
            store.close()
        if installation is None:
            raise HTTPException(status_code=404, detail="no such GitHub connection")
        app_ = _configured_github_app()
        signing = _state_key()
        assert token is not None
        state = State(
            org=org_id,
            client=installation.client,
            subject=token.subject,
            installation=installation_id,
        )
        return {"url": app_.authorize_url(sign_state(signing, state))}

    @app.post("/api/github/authorize")
    def authorize_github(body: dict[str, Any], token: Token | None = needs_admin) -> Any:
        """GitHub came back with an installation: now ask it who this person is."""
        from dataclasses import replace

        from flotta.github_app import sign_state

        state = _started_by_caller(token, body.get("state"))
        installation = str(body.get("installation_id") or "").strip()
        if not installation.isdigit():
            raise HTTPException(status_code=422, detail="GitHub named no installation")
        app_ = _configured_github_app()
        return {
            "url": app_.authorize_url(
                sign_state(_state_key(), replace(state, installation=installation))
            )
        }

    @app.post("/api/github/claim")
    def claim_github(body: dict[str, Any], token: Token | None = needs_admin) -> Any:
        """Connect the installation, if GitHub says this person owns its account."""
        from flotta.github_app import GitHubAppError, InstallationGone, NotProven
        from flotta.store import GitHubAlreadyConnectedError

        state = _started_by_caller(token, body.get("state"))
        if not state.installation:
            raise HTTPException(status_code=422, detail="this link names no installation")
        app_ = _configured_github_app()
        try:
            account = app_.proven_account(state.installation, str(body.get("code") or ""))
        except InstallationGone as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except NotProven as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except GitHubAppError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        assert token is not None
        store = store_factory()
        try:
            _org_or_404(store, state.org)
            if state.client is not None and store.get_client(state.org, state.client) is None:
                raise HTTPException(status_code=404, detail=f"no client {state.client!r}")
            held = store.connect_github(
                state.installation,
                org=state.org,
                client=state.client,
                account=account.login,
                account_type=account.type,
                connected_by=token.subject,
                connected_login=account.person.login if account.person else None,
                connected_user_id=account.person.id if account.person else None,
                connected_name=account.person.name if account.person else None,
            )
        except GitHubAlreadyConnectedError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        finally:
            store.close()
        # Somebody who just picked repositories on GitHub should see them on the
        # next open, not a minute later (FLOTTA-193).
        app_.forget_repositories(state.installation)
        _refresh_authors(held.org, held.client, "connected")
        return {"org": held.org, "client": held.client, "account": held.account}

    def _refresh_authors(org: str, client: str | None, reason: str) -> None:
        """Rename every settled box a connection could name, after it changed.

        The author is written into a machine when the box is created, so
        without this a reconnect as somebody else or a disconnect reached only
        boxes made afterwards (FLOTTA task 108). On a thread, for the reason
        repairs are: a box that is awake restarts, and the request that
        changed the connection has already been answered by the store. Each
        box's outcome is its own timeline's `commit_author` or
        `commit_author_refresh_failed`.
        """
        from flotta.provision import refresh_authors_in

        def run() -> None:
            inner = store_factory()
            try:
                refresh_authors_in(inner, org, client, reason=reason)
            except Exception as exc:  # noqa: BLE001
                _log.warning("refreshing %s's authors failed: %s: %s", org, type(exc).__name__, exc)
            finally:
                inner.close()

        if not background:
            run()
            return
        worker = threading.Thread(target=run, name=f"authors-{org}", daemon=True)
        _provisioning.add(worker)
        worker.start()

    @app.delete("/api/orgs/{org_id}/github/{installation_id}")
    def disconnect_github(
        org_id: str, installation_id: str, token: Token | None = needs_admin
    ) -> Any:
        """Remove the App from that account on GitHub, and forget it here.

        GitHub first, so a failure there leaves the connection showing rather
        than access Flotta no longer mentions. Grants stay, and mint nothing
        until the account is connected again.
        """
        from flotta.github_app import GitHubAppError

        _admin_of(token, org_id)
        store = store_factory()
        try:
            held = store.github_installation(org_id, installation_id)
            if held is None:
                raise HTTPException(status_code=404, detail="no such GitHub connection")
            app_ = _configured_github_app()
            try:
                app_.uninstall(installation_id)
            except GitHubAppError as exc:
                raise HTTPException(status_code=502, detail=str(exc)) from exc
            store.disconnect_github(org_id, installation_id)
        finally:
            store.close()
        app_.forget_repositories(installation_id)
        # The grants ended with the row; the name on its boxes ends here.
        _refresh_authors(org_id, held.client, "disconnected")
        return {"disconnected": installation_id}

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

    @app.patch("/api/orgs/{org_id}/members/{user_id}")
    def change_member(
        org_id: str, user_id: str, body: dict[str, Any], token: Token | None = needs_admin
    ) -> Any:
        """Give someone already in an org another role: `{"role", "client"?}`.

        Both fields say what the membership becomes, so a client left out is
        "every client" rather than "as before". It takes effect on the person's
        next request, because a session re-reads its membership on every one.
        Not its last owner, as for a removal.
        """
        _admin_of(token, org_id)
        role = str(body.get("role") or "")
        client = body.get("client")
        client = str(client).strip() or None if client is not None else None
        store = store_factory()
        try:
            _org_or_404(store, org_id)
            changed = store.change_membership(user_id, org_id, role, client_id=client)
        except InvalidAccountError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except UnknownEntityError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except LastOwnerError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        finally:
            store.close()
        return {
            "changed": {
                "user": changed.user_id,
                "org": org_id,
                "role": changed.role,
                "client": changed.client,
            }
        }

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
        _deliver_invite(
            org_id,
            email,
            secret,
            invite_message(email, sign_in_url(link_base, secret), org.name, role),
        )
        return {"invited": email, "org": org_id, "role": role, "client": client}

    def _deliver_invite(org_id: str, email: str, secret: str, message: Message) -> None:
        """Send a new invite link, then withdraw the address's older ones (FLOTTA-144).

        In that order: until the new link has gone, an older one may be the
        person's only way in. One that cannot be sent is dropped and the older
        ones stay, so a failed send changes nothing. Withdrawing the older ones
        is what makes the newest the one grant: correcting a too-broad invite
        by sending another must not leave the broad link live.
        """
        assert deliver_link is not None
        try:
            deliver_link(message)
        except Exception as exc:  # noqa: BLE001 - said to the inviter, not swallowed
            _log.exception("could not deliver an invite to %s", email)
            store = store_factory()
            try:
                store.drop_login_link(secret)
            finally:
                store.close()
            raise HTTPException(
                status_code=502, detail=f"the invite to {email} could not be sent; try again"
            ) from exc
        store = store_factory()
        try:
            store.drop_other_invites(org_id, email, keep=secret)
        finally:
            store.close()

    # -- invites not yet opened (FLOTTA-144) -----------------------------------
    #
    # Once sent, an invite was invisible: nobody could see who had been asked
    # and not joined, send a lost link again, or take back one sent to the
    # wrong address. Owners only, like sending one; nothing secret-derived
    # ever leaves (`Invite` has no hash).

    @app.get("/api/orgs/{org_id}/invites")
    def list_invites(org_id: str, token: Token | None = needs_admin) -> Any:
        """The invites to `org_id` nobody has opened yet, newest first, one per address.

        Expired ones too, with `expired: true`: those are what resending is for.
        """
        from dataclasses import asdict

        _admin_of(token, org_id)
        store = store_factory()
        try:
            _org_or_404(store, org_id)
            invites = [asdict(i) for i in store.open_invites(org_id)]
        finally:
            store.close()
        return {"org": org_id, "invites": invites}

    @app.post("/api/orgs/{org_id}/invites/resend", status_code=202)
    def resend_invite(org_id: str, body: dict[str, Any], token: Token | None = needs_admin) -> Any:
        """A fresh link for an invite never opened: `{"email"}`. The old link stops working.

        Counted as a new invite against the same limits, so resending cannot be
        used to send more than inviting could; but only once there is something
        to resend, so a mistyped address spends nothing. Someone already in the
        org is refused as inviting them would be. The old link stops working
        only once the new one is delivered.
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
        store = store_factory()
        try:
            org = _org_or_404(store, org_id)
            invitee = store.get_user_by_email(email)
            if invitee is not None and store.membership(invitee.id, org_id) is not None:
                raise HTTPException(status_code=409, detail=f"{email} is already in {org_id!r}")
            if store.open_invite(org_id, email) is None:
                raise HTTPException(
                    status_code=404, detail=f"no open invite to {org_id!r} for {email}"
                )
            org_limit = invites_per_pending_org if org.status == "pending" else invites_per_org
            if not hit_all((per_address, email), (org_limit, org_id)):
                raise HTTPException(
                    status_code=429,
                    detail="too many invites sent; wait, then send again",
                    headers={"Retry-After": str(int(LINK_LIMIT_WINDOW_S))},
                )
            try:
                secret, invite = store.reissue_invite(org_id, email)
            except UnknownEntityError as exc:
                raise HTTPException(status_code=404, detail=str(exc)) from exc
            except OrgNotActiveError as exc:
                raise HTTPException(status_code=403, detail=str(exc)) from exc
        finally:
            store.close()
        _deliver_invite(
            org_id,
            email,
            secret,
            invite_message(email, sign_in_url(link_base, secret), org.name, invite.role),
        )
        return {"invited": email, "org": org_id, "role": invite.role, "client": invite.client}

    @app.post("/api/orgs/{org_id}/invites/revoke")
    def revoke_invite(org_id: str, body: dict[str, Any], token: Token | None = needs_admin) -> Any:
        """Take back the invites to `org_id` for `{"email"}`: their links stop working now.

        One opened a moment ago whose person has not joined yet is taken back
        too (`withdraw_invites`); 404 when nothing is left to take back.
        """
        _admin_of(token, org_id)
        try:
            email = normalize_email(str(body.get("email") or ""))
        except InvalidAccountError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        store = store_factory()
        try:
            _org_or_404(store, org_id)
            gone = store.withdraw_invites(org_id, email)
        finally:
            store.close()
        if gone == 0:
            raise HTTPException(status_code=404, detail=f"no open invite to {org_id!r} for {email}")
        return {"revoked": email, "org": org_id}

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

    def _budget_reader(token: Token | None, org_id: str) -> None:
        """The org's own, or the operator: not a client's viewer, or an agent.

        What an agency allows a client's agents is the agency's business.
        """
        if _is_operator(token):
            return
        assert token is not None
        if (token.org or DEFAULT_ORG) != org_id or subject_box(token.subject) is not None:
            raise HTTPException(status_code=404, detail=f"no org {org_id!r}")
        if token.client is not None:
            raise HTTPException(status_code=403, detail="budgets are the agency's to see")

    @app.get("/api/orgs/{org_id}/budgets")
    def list_budgets(org_id: str, token: Token | None = needs_read) -> Any:
        """An org's budgets, and how far each has got this period. See `flotta.budgets`.

        Owners and members read. Each sums the ledger, as a budget check does.
        """
        _budget_reader(token, org_id)
        from flotta.budgets import standing

        now = datetime.now(UTC)
        store = store_factory()
        try:
            _org_or_404(store, org_id)
            standings = [standing(store, b, now) for b in store.budgets_for_org(org_id)]
            return {
                "org": org_id,
                "budgets": [
                    {
                        **s.details(),
                        "unpriced": s.unpriced,
                        "reached": s.reached,
                        "soft_reached": s.soft_reached,
                    }
                    for s in standings
                ],
            }
        finally:
            store.close()

    @app.get("/api/orgs/{org_id}/usage")
    def org_usage(
        org_id: str,
        client: str | None = None,
        box: str | None = None,
        group: str | None = None,
        from_: str | None = Query(default=None, alias="from"),
        to: str | None = None,
        token: Token | None = needs_read_or_govern,
    ) -> Any:
        """What an org has spent, added up, for whoever may read its budgets.

        `?group=day,client,box,kind,model,requested_model`, any of them, and `from` and
        `to` as days or moments, this month so far if not named. Never the ledger's rows:
        see `flotta.usage_report`. Grouped by `client`, each row names it (`client_name`).

        Also the hosted product's billing (`org:govern`), for any org: the totals
        an invoice is built from, and nothing more (FLOTTA-188).
        """
        if not (token is not None and is_governor(token)):
            _budget_reader(token, org_id)
        from flotta.usage_report import BadReport, parse_group, parse_window, report

        try:
            groups = parse_group(group)
            since, until = parse_window(from_, to, datetime.now(UTC))
        except BadReport as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        store = store_factory()
        try:
            _org_or_404(store, org_id)
            return report(
                store, org_id, since=since, until=until, group=groups, client=client, box=box
            )
        finally:
            store.close()

    @app.get("/api/orgs/{org_id}/budget-events")
    def list_budget_events(
        org_id: str, client: str | None = None, token: Token | None = needs_read
    ) -> Any:
        """What an org's budgets did, or one client's: its own and the org-wide ones."""
        _budget_reader(token, org_id)
        from dataclasses import asdict

        store = store_factory()
        try:
            return {"events": [asdict(e) for e in store.budget_events(org_id, client=client)]}
        finally:
            store.close()

    @app.get("/api/orgs/{org_id}/alerts")
    def get_alerts(org_id: str, token: Token | None = needs_read) -> Any:
        """Where an org's alerts go, and how the latest went. Never the webhook's secret."""
        _budget_reader(token, org_id)
        from dataclasses import asdict

        store = store_factory()
        try:
            _org_or_404(store, org_id)
            settings = store.org_alerts(org_id)
            return {
                "alert_email": settings.alert_email if settings else None,
                "webhook_url": settings.webhook_url if settings else None,
                "deliveries": [asdict(d) for d in store.alert_deliveries(org_id)],
            }
        finally:
            store.close()

    @app.put("/api/orgs/{org_id}/alerts")
    def put_alerts(org_id: str, body: dict[str, Any], token: Token | None = needs_admin) -> Any:
        """Where an org's alerts go, besides its owners, and only what the body names.

        `{"alert_email": "ops@example.com", "webhook_url": "https://example.com/hook"}`,
        and `null` removes either. **Setting a webhook answers with `webhook_secret`, this
        once:** setting it again, even to the same URL, makes a new one. A new URL takes
        over any alert still being tried. `docs/ALERTS.md`.
        """
        _admin_of(token, org_id)
        import secrets

        from flotta.auth import webhook_secret

        store = store_factory()
        try:
            _org_or_404(store, org_id)
            current = store.org_alerts(org_id)
            email = current.alert_email if current else None
            url = current.webhook_url if current else None
            nonce = current.webhook_nonce if current else None
            if "alert_email" in body:
                raw = body["alert_email"]
                try:
                    email = normalize_email(str(raw)) if raw else None
                except InvalidAccountError as exc:
                    raise HTTPException(status_code=422, detail=str(exc)) from exc
            minted = False
            if "webhook_url" in body:
                raw = body["webhook_url"]
                if raw:
                    try:
                        url = check_webhook_url(str(raw), allow_local=local_webhooks)
                    except WebhookRefused as exc:
                        raise HTTPException(status_code=422, detail=str(exc)) from exc
                    nonce, minted = secrets.token_hex(16), True
                else:
                    url, nonce = None, None
            store.set_org_alerts(org_id, alert_email=email, webhook_url=url, nonce=nonce)
            answer: dict[str, Any] = {"alert_email": email, "webhook_url": url}
            if minted:
                answer["webhook_secret"] = webhook_secret(key, org_id, nonce)
            return answer
        finally:
            store.close()

    @app.put("/api/orgs/{org_id}/budgets")
    def put_budget(org_id: str, body: dict[str, Any], token: Token | None = needs_admin) -> Any:
        """Set the budget for a scope and period, replacing any that is there.

        `{"period": "day", "limit_eur": "20", "action": "pause"}`, for the whole
        org, or with `"client"`, or with `"box"` for one agent. Optional:
        `"soft_pct"`, `"fallback_model"`. What each action does at the limit is in
        `flotta.budgets`.
        """
        _admin_of(token, org_id)
        from flotta.budgets import budget_dict, parse_limit
        from flotta.inference import InvalidModel
        from flotta.provision import allowed_model
        from flotta.store import InvalidBudgetError, UnknownEntityError

        for field in ("client", "box", "fallback_model"):
            if body.get(field) is not None and not isinstance(body[field], str):
                raise HTTPException(status_code=422, detail=f"{field} must be text")
        soft_pct = body.get("soft_pct", 0.8)
        if isinstance(soft_pct, bool) or not isinstance(soft_pct, int | float):
            raise HTTPException(status_code=422, detail="soft_pct must be a fraction, like 0.8")
        store = store_factory()
        try:
            _org_or_404(store, org_id)
            if body.get("fallback_model") is not None:
                # The gateway would refuse it at the limit: say so now, not then.
                allowed_model(body["fallback_model"], store)
            budget = store.set_budget(
                org_id,
                client=body.get("client"),
                box=body.get("box"),
                period=str(body.get("period")),
                limit_eur_nano=parse_limit(body.get("limit_eur")),
                action=str(body.get("action")),
                soft_pct=float(soft_pct),
                fallback_model=body.get("fallback_model"),
            )
            return budget_dict(budget)
        except (InvalidBudgetError, InvalidModel) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except UnknownEntityError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        finally:
            store.close()

    @app.delete("/api/orgs/{org_id}/budgets")
    def delete_budget(
        org_id: str,
        period: str,
        client: str | None = None,
        box: str | None = None,
        token: Token | None = needs_admin,
    ) -> Any:
        """Remove the budget for a scope and period, named as `PUT` names it."""
        _admin_of(token, org_id)
        store = store_factory()
        try:
            _org_or_404(store, org_id)
            if not store.delete_budget(org_id, period=period, client=client, box=box):
                raise HTTPException(status_code=404, detail="no such budget")
            return {"deleted": True}
        finally:
            store.close()

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
        if subject_box(subject) is not None or subject in ("relay", "billing"):
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
        # An org whose seats filled since the invite was sent answers 402 here,
        # before the link is touched. This is only the early answer: the check
        # that holds is inside `join_by_invite`, which spends the link and
        # takes the seat in one write, so a seat taken after this line still
        # leaves the invite there for when the limit is raised (FLOTTA-183).
        if link.role in SEAT_ROLES and (user is None or store.membership(user.id, org.id) is None):
            store.require_room(org.id, "seats")
        try:
            joined = store.join_by_invite(offered)
            if joined is None:
                raise refused
            _link, user, _membership = joined
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
            from flotta.mcp import catalogue_of as mcp_catalogue_of

            before = mcp_catalogue_of(store)[0] if "FLOTTA_MCP_SERVERS" in cleaned else None
            # All or nothing, so "refused as a whole" covers a database failure
            # and not only bad input.
            store.set_settings(cleaned)
            if before is not None:
                _mcp_catalogue_changed(store, before)
            return {"settings": describe(store)}
        finally:
            store.close()

    def _mcp_catalogue_changed(store: Any, before: dict[str, Any]) -> None:
        """What changing the MCP catalogue sets going (FLOTTA-125).

        - **A client secret goes with its server:** one removed from the list, or
          whose `client_id` changed, loses the secret set for it, which belonged
          to that registration and no other.
        - **Every agent granted a server that went, came back or changed** has its
          config written again, in the background: an entry taken out or put back
          and the agent reloaded, or new `tools.include`. Its grants are never
          written (`provision.rewrite_mcp_configs`), and a sleeping agent isn't
          woken: it is written when it next wakes.
        """
        from flotta.mcp import catalogue_of as mcp_catalogue_of
        from flotta.provision import rewrite_mcp_configs

        after = mcp_catalogue_of(store)[0]
        # Gone or changed, and come back: a server removed and added again (or the
        # override cleared back to the environment's) has grantees to write it to.
        names = list(dict.fromkeys([*before, *after]))
        changed = [
            n
            for n in names
            if n not in before or n not in after or after[n].spec() != before[n].spec()
        ]
        for name, server in before.items():
            moved = after.get(name)
            same_client = (
                moved is not None
                and moved.oauth is not None
                and server.oauth is not None
                and moved.oauth.client_id == server.oauth.client_id
            )
            if not same_client:
                store.delete_mcp_server_secret(name)
        if not changed:
            return

        def run() -> None:
            inner = store_factory()
            try:
                rewrite_mcp_configs(changed, store=inner)
            except Exception as exc:  # noqa: BLE001
                _log.warning("rewriting MCP configs failed: %s: %s", type(exc).__name__, exc)
            finally:
                inner.close()

        if not background:
            run()
            return
        worker = threading.Thread(target=run, name="mcp-catalogue", daemon=True)
        _provisioning.add(worker)
        worker.start()

    @app.post("/api/prices/refresh")
    def refresh_prices(token: Token | None = needs_write) -> Any:
        """Bring `model_prices` up to date with OpenRouter's catalogue (FLOTTA-87).

        Here because the prices live in the deployed store, which a laptop has
        no way to reach. A catalogue that cannot be read changes nothing.
        """
        _require_operator(token, "refresh the model prices")
        from flotta.model_catalog import CatalogueUnavailable, fetch_prices

        try:
            prices, skipped = fetch_prices()
        except CatalogueUnavailable as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        store = store_factory()
        try:
            changed = store.refresh_prices(prices)
        finally:
            store.close()
        return {"models": len(prices), "changed": changed, "skipped": skipped}

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
        # A read, so a stale image with a refresh behind it will do (FLOTTA-182).
        fleet_image, source = resolve_fleet_image(stale_ok=True)

        # What the build app last released, reported even when the environment
        # won — because the two disagreeing *is* the trap. A deployment
        # variable naming an image older than the newest build makes the app
        # offer the wrong upgrade with total confidence, and there is nowhere
        # else a person could notice.
        newest = None
        app = (os.environ.get("FLOTTA_FLY_APP") or "").strip() or None
        if app:
            newest = _newest_release_image(app, stale_ok=True)

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
        from flotta.provision import (
            box_sizes,
            fleet_model,
            model_changes,
            model_given_by_default,
            size_changes,
        )

        ids = [b.id for b in boxes]
        metas = store.meta_for_boxes(ids)
        models = store.models_for_boxes(ids)
        given = store.models_given(ids)
        changes = model_changes(store, ids)
        sizes = box_sizes(store, ids)
        resizes = size_changes(store, ids)
        default = fleet_model(store)
        default_given = model_given_by_default()
        approvers = store.client_approvers(ids)
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
                            clients_answer_approvals=b.id in approvers,
                            model_change=changes.get(b.id),
                            memory_mb=sizes.get(b.id),
                            size_change=resizes.get(b.id),
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
            # The same early answer for an org at its limit on agents: the
            # store refuses the row as well, under its guard (FLOTTA-183).
            tenant_store.require_room(org, "agents")
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
        # Its size, which only an owner chooses (FLOTTA-157): absent is the
        # fleet's default, which anyone who may create may have.
        memory_mb: int | None = None
        if body.get("memory_mb") is not None:
            memory_mb = _checked_memory(body.get("memory_mb"))
            _require_owner(token, "choose an agent's size")

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
                memory_mb=memory_mb,
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
                    memory_mb=memory_mb,
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
        memory_mb: int | None = None,
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
            except LimitReachedError:
                raise  # 402, from its handler: not a conflict over the name
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
                create_box(
                    name,
                    store=inner,
                    box=box,
                    volume_gb=volume_gb,
                    region=region,
                    memory_mb=memory_mb,
                )
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
        from flotta.provision import OrgSuspended, ProvisionError
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
            except OrgSuspended as exc:
                # What the wake answers, so the web app reads one status (FLOTTA-184).
                raise HTTPException(status_code=402, detail=str(exc)) from exc
            except ProvisionError as exc:
                # 409, not 500: the fleet is fine and the request was
                # well-formed — this box could not be written to right now.
                raise HTTPException(status_code=409, detail=str(exc)) from exc
        finally:
            store.close()

    @app.get("/api/boxes/{box_id}/rules")
    def get_rules(box_id: str, token: Token | None = needs_write) -> Any:
        """What an agent must always do, never do, and be careful with (FLOTTA-124).

        The agency's own people only, like the instructions they sit beside. A
        client's people are bound by them and are not shown them here, though
        asking the agent may well read the guidance back: it is in its prompt.
        """
        from flotta.provision import rules_of

        store = store_factory()
        try:
            box = _scoped_box(store, token, box_id)
            read = rules_of(box.id, store=store)
            return {**read, "changed_at": _epoch(read["changed_at"])}
        finally:
            store.close()

    @app.put("/api/boxes/{box_id}/rules")
    def put_rules(box_id: str, body: dict[str, Any], token: Token | None = needs_write) -> Any:
        """Replace an agent's rules, on the machine first (`provision.set_rules`).

        A full replace: a list left out is emptied. The enforced half reaches the
        agent's next command, the guidance its next conversation.
        """
        from flotta.provision import OrgSuspended, ProvisionError, set_rules
        from flotta.rules import RulesError, parse_rules, rules_from_json

        store = store_factory()
        try:
            box = _scoped_box(store, token, box_id)
            try:
                stored = store.box_rules(box.id)
                current = rules_from_json(stored[0] if stored else None)
                rules = parse_rules(body, kept_tools=current.disabled_tools)
            except RulesError as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc
            by = token.subject if token is not None else "operator"
            try:
                written = set_rules(box.id, rules, store=store, by=by)
            except OrgSuspended as exc:
                raise HTTPException(status_code=402, detail=str(exc)) from exc
            except ProvisionError as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            return {**written, "changed_at": _epoch(written["changed_at"])}
        finally:
            store.close()

    # -- MCP servers (FLOTTA-125) -----------------------------------------------
    #
    # The catalogue is the operator's: the fleet setting `FLOTTA_MCP_SERVERS`,
    # stored or from the environment (`flotta.mcp`), read on every request, so a
    # change in the console is in force at once. A connection is an org's, made by its owners;
    # a grant is an agent's, made by the agency's people. A client's people do
    # neither. The gateway enforces all of it on every call (`gateway.mcp`).
    #
    # A connection may also be one client's own (slice 3), made by the same
    # owners, at `/api/orgs/{org}/clients/{client}/mcp/...`. That client's agents
    # use it ahead of the org's; no other agent can.

    from flotta.mcp import McpError, catalogue_of, connection_usable

    def _mcp_catalogue(store: Any) -> dict[str, Any]:
        """The servers on offer now. A list that can't be read is served empty (`catalogue_of`)."""
        servers, error = catalogue_of(store)
        if error:
            _log.error("the MCP catalogue could not be read: %s", error)
        return servers

    def _mcp_server_or_404(name: str) -> Any:
        store = store_factory()
        try:
            server = _mcp_catalogue(store).get(name)
        finally:
            store.close()
        if server is None:
            raise HTTPException(status_code=404, detail=f"no MCP server {name!r} in this fleet")
        return server

    def _agency_of(token: Token | None, org_id: str) -> None:
        """404 unless `token` is the operator's or one of the org's own people, org-wide."""
        if _is_operator(token):
            return
        assert token is not None
        if (
            (token.org or DEFAULT_ORG) != org_id
            or token.client is not None
            or subject_box(token.subject) is not None
        ):
            raise HTTPException(status_code=404, detail=f"no org {org_id!r}")

    def _client_or_404(store: Any, org_id: str, client_id: str | None) -> None:
        if client_id is not None and store.get_client(org_id, client_id) is None:
            raise HTTPException(status_code=404, detail=f"no client {client_id!r}")

    def _person(store: Any, subject: str | None) -> str | None:
        """Who a subject is, as a page names them: a person's name, else their email."""
        from flotta.auth import USER_SUBJECT_PREFIX

        if not subject:
            return None
        if subject == "operator":
            return "the operator"
        if not subject.startswith(USER_SUBJECT_PREFIX):
            return None
        user = store.get_user(subject[len(USER_SUBJECT_PREFIX) :])
        return (user.name or user.email) if user is not None else None

    def _mcp_connection(store: Any, found: Any, server: Any) -> dict[str, Any] | None:
        """A connection as the agency sees it: whether it works, never what it holds.

        `status` is `broken` whenever the gateway wouldn't use it
        (`flotta.mcp.connection_usable`), with why in `detail`, so the page and
        the gateway never disagree.
        """
        if found is None:
            return None
        why = connection_usable(found, server)
        return {
            "kind": found.kind,
            "status": "ok" if why is None else "broken",
            "detail": why,
            "connected_by": found.connected_by,
            "connected_by_name": _person(store, found.connected_by),
            "connected_at": _epoch(found.created_at),
            "updated_at": _epoch(found.updated_at),
        }

    def _seal(value: str, context: str) -> str:
        from flotta.sealed import SealError, seal

        try:
            return seal(value, context=context)
        except SealError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    def _unseal(value: str, context: str) -> str:
        from flotta.sealed import SealError, unseal

        try:
            return unseal(value, context=context)
        except SealError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    def _oauth_http() -> Any:
        import httpx

        return httpx.Client(transport=mcp_oauth_transport, trust_env=False)

    def _mcp_redirect_uri() -> str:
        control = (os.environ.get("FLOTTA_CONTROL_URL") or "").strip().rstrip("/")
        if not control:
            raise HTTPException(
                status_code=503,
                detail="signing in to an MCP server needs $FLOTTA_CONTROL_URL, this control "
                "plane's public address, for the provider to send the browser back to",
            )
        return f"{control}/api/mcp/oauth/callback"

    def _return_to(raw: Any) -> str:
        """Where the browser goes after the callback: the web app, and nowhere else."""
        base = link_base.rstrip("/")
        if raw is None or raw == "":
            return base
        if not isinstance(raw, str) or not (raw == base or raw.startswith(f"{base}/")):
            raise HTTPException(status_code=422, detail=f"return_to must be under {base}")
        return raw

    @app.get("/api/mcp/servers")
    def list_mcp_servers(token: Token | None = needs_read) -> Any:
        """The operator's catalogue: what an agency can pick from."""
        store = store_factory()
        try:
            servers, error = catalogue_of(store)
        finally:
            store.close()
        return {"servers": [s.public() for s in servers.values()], "error": error}

    @app.get("/api/mcp/catalogue")
    def mcp_catalogue_for_operator(token: Token | None = needs_read) -> Any:
        """The catalogue as the console edits it: every field, where it came from, and
        whether each OAuth server has a client secret set. Never the secret.

        `source` is the setting's (`store`, `env` or `default`), as `GET /api/settings`
        says for the model list. Saved with `PUT /api/settings`, like every setting.
        """
        _require_operator(token, "read the MCP catalogue")
        from flotta.settings import describe

        store = store_factory()
        try:
            servers, error = catalogue_of(store)
            source = next(
                (d["source"] for d in describe(store) if d["key"] == "FLOTTA_MCP_SERVERS"),
                "default",
            )
        finally:
            store.close()
        return {
            "source": source,
            "servers": [s.for_operator() for s in servers.values()],
            "error": error,
        }

    @app.put("/api/mcp/servers/{server}/client-secret")
    def set_mcp_client_secret(
        server: str, body: dict[str, Any], token: Token | None = needs_write
    ) -> Any:
        """Set the OAuth client secret the operator registered `server` with.

        Kept out of the setting and sealed here, so the catalogue can be shown,
        copied and saved without carrying a secret. Never read back.
        """
        from flotta.mcp import parse_client_secret, server_secret_context

        _require_operator(token, "set an MCP server's client secret")
        found = _mcp_server_or_404(server)
        try:
            value = parse_client_secret(body, found)
        except McpError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        sealed = _seal(value, server_secret_context(server))
        store = store_factory()
        try:
            store.set_mcp_server_secret(
                server, sealed, by=token.subject if token is not None else "operator"
            )
        finally:
            store.close()
        return {"server": server, "has_client_secret": True}

    @app.delete("/api/mcp/servers/{server}/client-secret")
    def delete_mcp_client_secret(server: str, token: Token | None = needs_write) -> Any:
        _require_operator(token, "remove an MCP server's client secret")
        store = store_factory()
        try:
            gone = store.delete_mcp_server_secret(server)
        finally:
            store.close()
        if not gone:
            raise HTTPException(status_code=404, detail=f"{server} has no client secret set")
        return {"server": server, "has_client_secret": False}

    @app.get("/api/orgs/{org_id}/mcp")
    def org_mcp(org_id: str, token: Token | None = needs_write) -> Any:
        """Every catalogue server, and whether this org has connected it."""
        _agency_of(token, org_id)
        store = store_factory()
        try:
            _org_or_404(store, org_id)
            servers = _mcp_catalogue(store)
            connected = {c.server: c for c in store.mcp_connections(org_id)}
            by_client: dict[str, dict[str, Any]] = {}
            for c in store.mcp_client_connections(org_id):
                by_client.setdefault(c.server, {})[c.client_id] = _mcp_connection(
                    store, c, servers.get(c.server)
                )
            return {
                "servers": [
                    {
                        **s.public(),
                        "connection": _mcp_connection(store, connected.get(s.name), s),
                        "clients": by_client.get(s.name, {}),
                    }
                    for s in servers.values()
                ]
            }
        finally:
            store.close()

    @app.get("/api/orgs/{org_id}/clients/{client_id}/mcp")
    def client_mcp(org_id: str, client_id: str, token: Token | None = needs_write) -> Any:
        """Every catalogue server, this client's own connection, and the agency's under it."""
        _agency_of(token, org_id)
        store = store_factory()
        try:
            _org_or_404(store, org_id)
            _client_or_404(store, org_id, client_id)
            servers = _mcp_catalogue(store)
            own = {c.server: c for c in store.mcp_connections(org_id, client=client_id)}
            agency = {c.server: c for c in store.mcp_connections(org_id)}
            return {
                "client": client_id,
                "servers": [
                    {
                        **s.public(),
                        "connection": _mcp_connection(store, own.get(s.name), s),
                        "agency": _mcp_connection(store, agency.get(s.name), s),
                    }
                    for s in servers.values()
                ],
            }
        finally:
            store.close()

    def _connect_key(
        org_id: str, client_id: str | None, server: str, body: Any, token: Token | None
    ) -> Any:
        import json

        from flotta.mcp import connection_context, parse_key

        _admin_of(token, org_id)
        found = _mcp_server_or_404(server)
        try:
            key_ = parse_key(body, found)
        except McpError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        store = store_factory()
        try:
            _org_or_404(store, org_id)
            _client_or_404(store, org_id, client_id)
            sealed = _seal(
                json.dumps({"kind": found.auth, "value": key_}),
                connection_context(org_id, server, client_id),
            )
            made = store.set_mcp_connection(
                org_id,
                server,
                kind=found.auth,
                credential=sealed,
                by=token.subject if token is not None else "operator",
                client=client_id,
                server_url=found.url,
            )
            answer = {"server": server, "connection": _mcp_connection(store, made, found)}
        finally:
            store.close()
        return {**answer, "client": client_id} if client_id else answer

    @app.put("/api/orgs/{org_id}/mcp/{server}")
    def connect_mcp_key(
        org_id: str, server: str, body: dict[str, Any], token: Token | None = needs_admin
    ) -> Any:
        """Connect a server with a key: `{"key": "..."}`. Sealed; never answered back."""
        return _connect_key(org_id, None, server, body, token)

    @app.put("/api/orgs/{org_id}/clients/{client_id}/mcp/{server}")
    def connect_client_mcp_key(
        org_id: str,
        client_id: str,
        server: str,
        body: dict[str, Any],
        token: Token | None = needs_admin,
    ) -> Any:
        """Connect a server with a client's own key, for that client's agents only."""
        return _connect_key(org_id, client_id, server, body, token)

    @app.post("/api/orgs/{org_id}/mcp/{server}/connect")
    def connect_mcp_oauth(
        org_id: str, server: str, body: dict[str, Any], token: Token | None = needs_admin
    ) -> Any:
        """Where to send an owner to sign in to an OAuth server (`flotta.mcp_oauth`).

        `{"return_to": "<web app URL>"}` is where the browser comes back to, with
        `mcp_state` to claim. Nothing is connected until the claim.
        """
        return _start_sign_in(org_id, None, server, body, token)

    @app.post("/api/orgs/{org_id}/clients/{client_id}/mcp/{server}/connect")
    def connect_client_mcp_oauth(
        org_id: str,
        client_id: str,
        server: str,
        body: dict[str, Any],
        token: Token | None = needs_admin,
    ) -> Any:
        """The same sign-in, ending as one client's own connection."""
        return _start_sign_in(org_id, client_id, server, body, token)

    def _start_sign_in(
        org_id: str, client_id: str | None, server: str, body: Any, token: Token | None
    ) -> Any:
        import json
        import secrets as secrets_
        from datetime import timedelta

        from flotta import mcp_oauth

        _admin_of(token, org_id)
        found = _mcp_server_or_404(server)
        if found.auth != "oauth":
            raise HTTPException(status_code=422, detail=f"{server} is connected with a key")
        if not key:
            raise HTTPException(status_code=503, detail="signing in needs $FLOTTA_SIGNING_KEY set")
        return_to = _return_to(body.get("return_to"))
        redirect_uri = _mcp_redirect_uri()
        try:
            with _oauth_http() as http:
                endpoints = mcp_oauth.discover(http, found)
                client = mcp_oauth.client_for(http, found, endpoints, redirect_uri)
        except mcp_oauth.OAuthError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        verifier, challenge = mcp_oauth.pkce()
        flow_id = f"mf-{secrets_.token_urlsafe(24)}"
        sealed = _seal(
            json.dumps(
                {
                    **json.loads(mcp_oauth.flow_payload(endpoints, client, verifier, redirect_uri)),
                    # What the sign-in is for: a claim after the entry moved stores nothing.
                    "server_url": found.url,
                }
            ),
            mcp_oauth.flow_context(flow_id),
        )
        store = store_factory()
        try:
            _org_or_404(store, org_id)
            _client_or_404(store, org_id, client_id)
            store.delete_mcp_flows_before(
                datetime.now(UTC) - timedelta(seconds=6 * mcp_oauth.STATE_TTL_S)
            )
            store.create_mcp_flow(
                flow_id,
                org_id=org_id,
                server=server,
                subject=token.subject if token is not None else "operator",
                sealed=sealed,
                return_to=return_to,
                client=client_id,
            )
        finally:
            store.close()
        state = mcp_oauth.sign_state(key, flow_id)
        return {
            "url": mcp_oauth.authorize_url(
                found,
                endpoints,
                client,
                redirect_uri=redirect_uri,
                state=state,
                challenge=challenge,
            )
        }

    def _live_flow(store: Any, raw: Any) -> Any:
        from flotta import mcp_oauth

        if not key:
            raise HTTPException(status_code=503, detail="signing in needs $FLOTTA_SIGNING_KEY set")
        try:
            flow_id = mcp_oauth.read_state(key, str(raw or ""))
        except mcp_oauth.BadState as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        flow = store.mcp_flow(flow_id)
        if flow is None:
            raise HTTPException(status_code=422, detail="that sign-in is finished or unknown")
        return flow

    @app.get("/api/mcp/oauth/callback")
    def mcp_oauth_callback(
        state: str = "", code: str = "", error: str = "", error_description: str = ""
    ) -> Any:
        """Where a provider sends the browser back. It changes nothing.

        **The code stays with the browser that signed in.** It is handed on to the
        web app, whose server presents it in a claim with that person's session,
        and only a claim by the person who started the sign-in trades it
        (`flotta.mcp_oauth`). Trading it here would leave tokens anyone holding
        the state could take, and the person who started it holds the state.

        The state is checked so a forged one is refused here, and the flow is
        left alone either way: this route has no caller to answer for.
        """
        from urllib.parse import urlencode

        from fastapi.responses import RedirectResponse

        store = store_factory()
        try:
            flow = _live_flow(store, state)
        finally:
            store.close()
        server = _mcp_server_or_404(flow.server)
        back = flow.return_to
        # Which client it was for, so the page can say so; not a secret.
        whose = {"mcp_client": flow.client_id} if flow.client_id else {}
        if error or not code:
            reason = error or "the provider sent no code"
            if back:
                query = urlencode({"mcp_server": server.name, **whose, "mcp_error": reason})
                return RedirectResponse(f"{back}{'&' if '?' in back else '?'}{query}", 303)
            raise HTTPException(status_code=400, detail=f"{server.title}: {reason}")
        if back:
            query = urlencode(
                {"mcp_server": server.name, **whose, "mcp_state": state, "mcp_code": code}
            )
            return RedirectResponse(f"{back}{'&' if '?' in back else '?'}{query}", 303)
        return {
            "server": server.name,
            "state": state,
            "code": code,
            "next": "POST /api/mcp/oauth/claim with this state and code, as whoever started it",
        }

    @app.post("/api/mcp/oauth/claim")
    def claim_mcp_oauth(body: dict[str, Any], token: Token | None = needs_admin) -> Any:
        """Trade a sign-in's code and keep it as the connection it was started for.

        `{"state", "code"}`. The flow says whose: the org's own, or one client's
        (`client` in the answer), as `connect_mcp_oauth` or
        `connect_client_mcp_oauth` started it.

        **Only the person who started it, and only then is the code spent**, as
        `claim_github` asks GitHub who someone is only after the same check. The
        flow is taken first, so one sign-in is claimed once.
        """
        import json

        from flotta import mcp_oauth
        from flotta.mcp import connection_context

        code = body.get("code")
        if not isinstance(code, str) or not code.strip():
            raise HTTPException(
                status_code=422, detail="a claim carries the code the provider sent"
            )
        store = store_factory()
        try:
            flow = _live_flow(store, body.get("state"))
            subject = token.subject if token is not None else "operator"
            if subject != flow.subject:
                raise HTTPException(
                    status_code=403,
                    detail="this sign-in was started by someone else; start again yourself",
                )
            _admin_of(token, flow.org_id)
            server = _mcp_server_or_404(flow.server)
            # A client removed since the start has nowhere to keep a connection.
            _client_or_404(store, flow.org_id, flow.client_id)
            payload = json.loads(_unseal(flow.sealed, mcp_oauth.flow_context(flow.id)))
            if not store.delete_mcp_flow(flow.id):
                raise HTTPException(status_code=422, detail="that sign-in is already claimed")
            if payload.get("server_url", server.url) != server.url:
                raise HTTPException(
                    status_code=409,
                    detail=f"{server.title} moved to another address during the sign-in; "
                    "start again",
                )
            endpoints = mcp_oauth.Endpoints(**payload["endpoints"])
            client = mcp_oauth.Client(**payload["client"])
            try:
                with _oauth_http() as http:
                    tokens = mcp_oauth.exchange(
                        http,
                        server,
                        endpoints,
                        client,
                        code=code.strip(),
                        verifier=payload["verifier"],
                        redirect_uri=payload["redirect_uri"],
                    )
            except mcp_oauth.OAuthError as exc:
                raise HTTPException(status_code=502, detail=str(exc)) from exc
            credential = mcp_oauth.oauth_credential(endpoints, client, tokens)
            sealed = _seal(
                json.dumps(credential),
                connection_context(flow.org_id, server.name, flow.client_id),
            )
            made = store.set_mcp_connection(
                flow.org_id,
                server.name,
                kind="oauth",
                credential=sealed,
                by=subject,
                client=flow.client_id,
                server_url=server.url,
            )
            return {
                "org": made.org_id,
                "client": flow.client_id,
                "server": server.name,
                "connection": _mcp_connection(store, made, server),
            }
        finally:
            store.close()

    @app.delete("/api/orgs/{org_id}/mcp/{server}")
    def disconnect_mcp(org_id: str, server: str, token: Token | None = needs_admin) -> Any:
        """Forget a connection, and ask the provider to revoke it where it can.

        Grants stay: an agent granted a server nobody has connected is told so
        by the gateway, and connecting it again needs no new grant.
        """
        return _disconnect(org_id, None, server, token)

    @app.delete("/api/orgs/{org_id}/clients/{client_id}/mcp/{server}")
    def disconnect_client_mcp(
        org_id: str, client_id: str, server: str, token: Token | None = needs_admin
    ) -> Any:
        """Forget a client's own connection: its agents go back to the agency's."""
        return _disconnect(org_id, client_id, server, token)

    def _disconnect(org_id: str, client_id: str | None, server: str, token: Token | None) -> Any:
        import json

        from flotta import mcp_oauth
        from flotta.mcp import connection_context

        _admin_of(token, org_id)
        found = _mcp_server_or_404(server)
        store = store_factory()
        try:
            _org_or_404(store, org_id)
            _client_or_404(store, org_id, client_id)
            gone = store.delete_mcp_connection(org_id, server, client=client_id)
        finally:
            store.close()
        if gone is None:
            raise HTTPException(status_code=404, detail=f"{server} isn't connected")
        revoked = False
        if gone.kind == "oauth":
            try:
                credential = json.loads(
                    _unseal(gone.credential, connection_context(org_id, server, client_id))
                )
                with _oauth_http() as http:
                    for value in (credential.get("refresh_token"), credential.get("access_token")):
                        if value:
                            revoked = (
                                mcp_oauth.revoke(
                                    http,
                                    found,
                                    mcp_oauth.endpoints_of(credential),
                                    mcp_oauth.client_of(credential),
                                    value,
                                )
                                or revoked
                            )
            except HTTPException:
                # Forgotten here either way; the provider's copy expires on its own.
                _log.warning("the %s connection of %s won't open to revoke", server, org_id)
        answer = {"server": server, "disconnected": True, "revoked": revoked}
        return {**answer, "client": client_id} if client_id else answer

    @app.get("/api/boxes/{box_id}/mcp")
    def get_mcp_grants(box_id: str, token: Token | None = needs_write) -> Any:
        """The MCP servers an agent may call, which tools, and whether calls ask first.

        `accounts` says, per granted server, whose connection its calls go out
        on: its client's own, else the agency's, chosen as the gateway chooses
        (`gateway.mcp.Proxy.resolve`), with `status` `ok` only when the gateway
        would use it. A connection of another kind than the catalogue now says
        (or for a server no longer offered) is `broken`, and a client's that
        can't be used is still the client's: the gateway doesn't fall back.
        `null` when neither is connected.
        """
        from flotta.provision import mcp_grants_of

        store = store_factory()
        try:
            box = _scoped_box(store, token, box_id)
            read = mcp_grants_of(box.id, store=store)
            servers = _mcp_catalogue(store)
            accounts: dict[str, Any] = {}
            for granted in read["servers"]:
                name = granted["server"]
                own = store.mcp_connection(box.org, name, client=box.client)
                found = own or store.mcp_connection(box.org, name)
                if found is None:
                    accounts[name] = None
                    continue
                why = connection_usable(found, servers.get(name))
                accounts[name] = {
                    "account": "client" if own else "org",
                    "status": "ok" if why is None else "broken",
                    "detail": why,
                }
        finally:
            store.close()
        return {**read, "accounts": accounts, "changed_at": _epoch(read["changed_at"])}

    @app.put("/api/boxes/{box_id}/mcp")
    def put_mcp_grants(box_id: str, body: dict[str, Any], token: Token | None = needs_write) -> Any:
        """Replace an agent's grants: in force at the gateway at once, then written to it.

        `written: false` says the agent's config is behind (`provision.set_mcp_grants`).
        """
        from flotta.mcp import parse_grants
        from flotta.provision import ProvisionError, set_mcp_grants

        store = store_factory()
        try:
            box = _scoped_box(store, token, box_id)
            try:
                grants = parse_grants(body, _mcp_catalogue(store))
            except McpError as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc
            by = token.subject if token is not None else "operator"
            try:
                written = set_mcp_grants(box.id, grants, store=store, by=by)
            except ProvisionError as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
        finally:
            store.close()
        return {**written, "changed_at": _epoch(written["changed_at"])}

    @app.get("/api/boxes/{box_id}/mcp/calls")
    def get_mcp_calls(box_id: str, limit: int = 100, token: Token | None = needs_write) -> Any:
        """An agent's tool calls, newest first: which tool, and what became of it."""
        store = store_factory()
        try:
            box = _scoped_box(store, token, box_id)
            calls = store.mcp_calls(box.id, limit=max(1, min(limit, 500)))
        finally:
            store.close()
        return {
            "calls": [
                {
                    "server": c.server,
                    "tool": c.tool,
                    "status": c.status,
                    "account": c.account,
                    "at": _epoch(c.at),
                }
                for c in calls
            ]
        }

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

        Not polled, and it must not become polled: it is a call to Fly each
        time. The list view stays on the store, which is cheap and, for status,
        kept current by every verb that touches a box.

        Timed on `MACHINE_TIMING_LOG` (FLOTTA-182): how long asking the
        substrate took, and how long finding the fleet's image took, which
        without `$FLOTTA_FLY_IMAGE` was a `flyctl releases` of 1.0–1.5 s once
        a minute. It is a read, so it takes a stale image and lets the refresh
        run behind it (`provision._RELEASE_STALE_MAX_S`); an upgrade still
        waits for a fresh one.
        """
        from flotta.backend import BackendError, backend_for
        from flotta.provision import _fleet_image, _same_image

        def timed_fleet_image(inspect_ms: float, outcome: str) -> str | None:
            started = time.perf_counter()
            image = _fleet_image(stale_ok=True)
            logging.getLogger(MACHINE_TIMING_LOG).info(
                "machine read for %s: %s in %.0f ms, fleet image in %.0f ms",
                box.id,
                outcome,
                inspect_ms,
                (time.perf_counter() - started) * 1000,
            )
            return image

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
                    "fleet_image": _fleet_image(stale_ok=True),
                    "image_current": None,
                }
            asked = time.perf_counter()
            try:
                info = backend_for(box.endpoint).inspect(box.endpoint)
            except (BackendError, OSError, NotImplementedError) as exc:
                return {
                    "box": row,
                    "machine": None,
                    "unavailable": str(exc),
                    "fleet_image": timed_fleet_image(
                        (time.perf_counter() - asked) * 1000, "unavailable"
                    ),
                    "image_current": None,
                }
            inspect_ms = (time.perf_counter() - asked) * 1000
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
            fleet_image = timed_fleet_image(inspect_ms, info.state)
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

    @app.get("/api/boxes/{box_id}/repos")
    def list_repos(box_id: str, token: Token | None = needs_read) -> Any:
        """Which repositories a box may use."""
        store = store_factory()
        try:
            box = _operated_box(store, token, box_id)
            # A grant outlives its source: from before FLOTTA-83, or on a GitHub
            # account since disconnected. It mints nothing, so naming it would be
            # a repository the agent is told it has and is refused when it asks.
            return {"box_id": box.id, "name": box.name, "repos": store.usable_repos(box.id)}
        finally:
            store.close()

    def _check_installation_covers(installation: Any, repo: str) -> None:
        """422 if GitHub says this installation does not cover `repo`.

        Allowed when GitHub cannot say, as `grant_repo_endpoint` explains.
        """
        from flotta.github_app import GitHubAppError

        app_ = _configured_github_app()
        try:
            covering = app_.installation_of(repo)
        except GitHubAppError:
            return
        if covering != installation.installation_id:
            raise HTTPException(
                status_code=422,
                detail=f"{repo} is not among the repositories selected for Flotta on "
                f"{installation.account}. Add it there on GitHub, then grant it here.",
            )

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

        **Whose call depends on whose GitHub it is.** On a GitHub account the
        agency connected (FLOTTA-22), it is the agency's: that is its own code.
        On `$FLOTTA_GITHUB_TOKEN` it is the operator's alone, for the operator's
        agents, because that token is the operator's (FLOTTA-83) and `fleet:write`
        in an agency's org was an agency granting itself the operator's
        repositories. Every refusal comes before GitHub is asked.

        **The grant remembers which it was.** One made on a connected account
        never falls back to the fleet token when that account is disconnected,
        because nobody with the right to spend that token authorised it.
        """
        from flotta.store import normalise_repo

        store = store_factory()
        try:
            box = _operated_box(store, token, box_id)
            connected = store.github_installations_for(box.org, box.client)
            if not connected and not reaches_fleet_github(box.org):
                raise HTTPException(status_code=403, detail=AGENCY_GITHUB_UNAVAILABLE)
            # Normalised first, because the check asks GitHub about a path: a
            # pasted URL or a `.git` suffix would ask about a repository that
            # does not exist and read as a denial.
            try:
                wanted = normalise_repo(str(body.get("repo") or ""))
            except ValueError as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc

            installation = store.github_installation_for(box.org, box.client, wanted)
            if installation is not None:
                _check_installation_covers(installation, wanted)
            elif not reaches_fleet_github(box.org):
                accounts = ", ".join(i.account for i in connected)
                raise HTTPException(
                    status_code=422,
                    detail=f"{wanted} is not on a GitHub account connected for this agent "
                    f"({accounts}). Connect its owner first.",
                )
            else:
                _require_operator(token, "grant a repository")
                check = reachable or _default_reachable
                reach = check(wanted, token=(os.environ.get(GITHUB_TOKEN_ENV) or "").strip())
                if reach.refuses:
                    raise HTTPException(status_code=422, detail=reach.detail)

            fleet_token = installation is None
            try:
                repo = store.grant_repo(box.id, wanted, fleet_token=fleet_token)
            except ValueError as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc
            store.add_event(
                "box",
                box.id,
                "repo_granted",
                {"repo": repo, "source": "fleet_token" if fleet_token else "github_app"},
            )
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

    @app.put("/api/boxes/{box_id}/client-approvals")
    def set_client_approvals(
        box_id: str, body: dict[str, Any], token: Token | None = needs_write
    ) -> Any:
        """Whether this agent's client's people may answer its approvals (FLOTTA-124).

        Off unless the agency turns it on. It is signed into the next ticket, so a
        socket already open keeps what it was given until it reconnects, and "always"
        stays refused either way: `flotta.door.conversation`.
        """
        allowed = body.get("allowed")
        if not isinstance(allowed, bool):
            raise HTTPException(status_code=422, detail='send {"allowed": true} or false')
        store = store_factory()
        try:
            box = _scoped_box(store, token, box_id)
            store.set_clients_answer_approvals(box.id, allowed)
            store.add_event(
                "box",
                box.id,
                "client_approvals_set",
                {"allowed": allowed, "by": token.subject if token is not None else "operator"},
            )
            return {"box": _with_meta(store, box)}
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

            def change() -> None:
                # The guard is released whatever happens here, opening the
                # store included: only this thread releases it now, and a stuck
                # one refuses every later change until the process restarts.
                try:
                    inner = store_factory()
                    try:
                        set_model(box.id, model, store=inner)
                    except UpgradeFailed as exc:
                        # `set_model` recorded it.
                        _log.warning("changing %s's model failed: %s", box.name, exc)
                    finally:
                        inner.close()
                except Exception as exc:  # noqa: BLE001
                    # The box destroyed meanwhile, the store gone: recorded so
                    # the window does not say "restarting" until it goes stale.
                    _log.warning("changing %s's model failed: %s", box.name, exc)
                    _record_unfinished_change(store_factory, box.id, started["model"], exc)
                finally:
                    _model_changes_running.discard(box.id)

            handed_over = False
            try:
                if not background:
                    try:
                        result = set_model(box.id, model, store=store)
                    except UpgradeFailed as exc:
                        raise HTTPException(status_code=502, detail=str(exc)) from exc
                    except ProvisionError as exc:
                        raise HTTPException(status_code=409, detail=str(exc)) from exc
                    return {**result, "box": _with_meta(store, _scoped_box(store, token, box.id))}
                answer = {**started, "started": True, "box": _with_meta(store, box)}
                worker = threading.Thread(target=change, name=f"model-{box.name}", daemon=True)
                _provisioning.add(worker)
                worker.start()
                handed_over = True
                return JSONResponse(answer, status_code=202)
            except HTTPException:
                raise
            except Exception as exc:
                # It will never run: say so, rather than leave a change that
                # nothing is making showing as under way.
                _record_unfinished_change(store_factory, box.id, started["model"], exc)
                raise
            finally:
                if not handed_over:
                    _model_changes_running.discard(box.id)
        finally:
            store.close()

    @app.put("/api/boxes/{box_id}/size")
    def set_size_endpoint(
        box_id: str, body: dict[str, Any], token: Token | None = needs_write
    ) -> Any:
        """Give an agent's machine more or less memory (FLOTTA-157), keeping its disk.

        `{"memory_mb": 2048}`, one of `flotta.backend.MEMORY_SIZES_MB`; the CPUs
        follow from it on the substrate. **An owner's, or the operator's**, not
        a member's: the size is what the agency pays for the machine
        (`_require_owner`), so `fleet:write` alone is a 403.

        The model route's shape, for its reason (FLOTTA-99): a running agent
        restarts, which outlasts the web app's ten seconds, so the refusals
        come first, then `202` and a thread. The box's `size_change` says it is
        under way — the cue for an open conversation to expect the drop and
        reconnect — and the timeline's `size_set` or `size_change_failed` how
        it ended. An agent already that size is a `200` with `already_current`
        and no restart. A substrate that cannot resize (a local stand-in) is a
        `409`, and a size this machine cannot have a `422`, both before the 202.
        """
        from fastapi.responses import JSONResponse

        from flotta.provision import (
            InvalidSize,
            ProvisionError,
            UpgradeFailed,
            begin_resize,
            resize_box,
        )

        if "memory_mb" not in body:
            raise HTTPException(
                status_code=422, detail='how much memory? Send {"memory_mb": 2048}.'
            )
        memory_mb = _checked_memory(body.get("memory_mb"))
        store = store_factory()
        try:
            box = _scoped_box(store, token, box_id)
            _require_owner(token, "resize an agent")
            with _resizes_lock:
                if box.id in _resizes_running:
                    raise HTTPException(
                        status_code=409,
                        detail=f"{box.name} is already being resized; wait for that to finish.",
                    )
                try:
                    started = begin_resize(box.id, memory_mb, store=store)
                except InvalidSize as exc:
                    # On the list, and still not a size this machine can have
                    # (a performance CPU needs 2 GB of its own).
                    raise HTTPException(status_code=422, detail=str(exc)) from exc
                except ProvisionError as exc:
                    raise HTTPException(status_code=409, detail=str(exc)) from exc
                if started.get("already_current"):
                    return {
                        "box_id": box.id,
                        "memory_mb": memory_mb,
                        "already_current": True,
                        "box": _with_meta(store, box),
                    }
                _resizes_running.add(box.id)

            def resize() -> None:
                # Released whatever happens, opening the store included: only
                # this thread releases it now (see `set_model_endpoint`).
                try:
                    inner = store_factory()
                    try:
                        resize_box(box.id, memory_mb, store=inner)
                    except UpgradeFailed as exc:
                        # `resize_box` recorded it.
                        _log.warning("resizing %s failed: %s", box.name, exc)
                    finally:
                        inner.close()
                except Exception as exc:  # noqa: BLE001
                    _log.warning("resizing %s failed: %s", box.name, exc)
                    _record_unfinished_resize(store_factory, box.id, memory_mb, exc)
                finally:
                    _resizes_running.discard(box.id)

            handed_over = False
            try:
                if not background:
                    try:
                        resize_box(box.id, memory_mb, store=store)
                    except UpgradeFailed as exc:
                        raise HTTPException(status_code=502, detail=str(exc)) from exc
                    except ProvisionError as exc:
                        raise HTTPException(status_code=409, detail=str(exc)) from exc
                    return {
                        "box_id": box.id,
                        "memory_mb": memory_mb,
                        "box": _with_meta(store, _scoped_box(store, token, box.id)),
                    }
                answer = {
                    "box_id": box.id,
                    "memory_mb": memory_mb,
                    "started": True,
                    "box": _with_meta(store, box),
                }
                worker = threading.Thread(target=resize, name=f"resize-{box.name}", daemon=True)
                _provisioning.add(worker)
                worker.start()
                handed_over = True
                return JSONResponse(answer, status_code=202)
            except HTTPException:
                raise
            except Exception as exc:
                # It will never run: say so, rather than leave "resizing" showing.
                _record_unfinished_resize(store_factory, box.id, memory_mb, exc)
                raise
            finally:
                if not handed_over:
                    _resizes_running.discard(box.id)
        finally:
            store.close()

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

    def _move_provider(box_id: str, token: Token | None, *, join: bool) -> Any:
        """The two routes below. `202`, then a thread: a running agent restarts twice."""
        from fastapi.responses import JSONResponse

        from flotta.provision import (
            OrgSuspended,
            ProvisionError,
            UpgradeFailed,
            gateway_url,
            join_gateway,
            leave_gateway,
            movable_box,
        )

        move = join_gateway if join else leave_gateway
        store = store_factory()
        try:
            box = _operated_box(store, token, box_id)
            if not _is_operator(token):
                raise HTTPException(
                    status_code=403, detail="only the fleet's operator may move an agent"
                )
            if join and not gateway_url():
                raise HTTPException(
                    status_code=409,
                    detail="there is no gateway. Set $FLOTTA_GATEWAY_URL where this runs.",
                )
            try:
                movable_box(box.id, store)
            except OrgSuspended as exc:
                raise HTTPException(status_code=402, detail=str(exc)) from exc
            except ProvisionError as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            if not background:
                try:
                    return move(box.id, store=store)
                except UpgradeFailed as exc:
                    raise HTTPException(status_code=502, detail=str(exc)) from exc
                except ProvisionError as exc:
                    raise HTTPException(status_code=409, detail=str(exc)) from exc
        finally:
            store.close()

        def run() -> None:
            inner = store_factory()
            try:
                move(box.id, store=inner)
            except Exception as exc:  # noqa: BLE001
                # A failed write leaves an event first; a refusal is why it was refused.
                _log.warning("moving %s failed: %s: %s", box.name, type(exc).__name__, exc)
            finally:
                inner.close()

        worker = threading.Thread(target=run, name=f"move-{box.name}", daemon=True)
        _provisioning.add(worker)
        worker.start()
        return JSONResponse({"box_id": box.id, "name": box.name, "started": True}, status_code=202)

    @app.post("/api/boxes/{box_id}/gateway")
    def join_gateway_endpoint(box_id: str, token: Token | None = needs_write) -> Any:
        """Move an agent onto the model gateway, and take the fleet's key off it.

        The operator's: this is the fleet's bill and the fleet's key. Its outcome is
        the timeline's `gateway_joined` or `gateway_move_failed`. See
        `provision.join_gateway`.
        """
        return _move_provider(box_id, token, join=True)

    @app.delete("/api/boxes/{box_id}/gateway")
    def leave_gateway_endpoint(box_id: str, token: Token | None = needs_write) -> Any:
        """Put an agent back on the provider directly: the way back."""
        return _move_provider(box_id, token, join=False)

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

    @app.post("/api/boxes/{box_id}/author")
    def refresh_author_endpoint(box_id: str, token: Token | None = needs_write) -> Any:
        """Re-decide whose name goes on an agent's commits, on its machine.

        A connect, a reconnect or a disconnect does this for every box of its
        scope by itself (`_refresh_authors`); this is the verb for the rest —
        a fleet PAT that changed hands, an agent from before FLOTTA-105 — and
        the one an agency owner has, since the runbook's `flyctl machine
        update` is the operator's. See `provision.refresh_author`.

        `fleet:write`, like renewing an identity: a running agent restarts.
        The operator reaches any org's agent (`_operated_box`). Synchronous,
        for the reason renewing an identity is: a sleeping agent, which is
        most of them, answers in seconds, and nothing is rewritten when the
        machine already says what the rule decides. A button in front of this
        should background it first, the way upgrades were.
        """
        from flotta.provision import ProvisionError, UpgradeFailed, refresh_author

        store = store_factory()
        try:
            box = _operated_box(store, token, box_id)
            try:
                return refresh_author(box.id, store=store)
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
            return {"box_id": box.id, "name": box.name, "repos": store.usable_repos(box.id)}
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

    # -- runs (M14.2) ----------------------------------------------------------
    #
    # Work an agent does on its own: asked for here, delivered by the run loop
    # (`flotta.runner`), read back with its steps and what it cost. The
    # agency's, and a client's own systems' (a token for that client with
    # `fleet:write`). A client's people see none of it (D21).

    def _run_reader(token: Token | None) -> None:
        """Not a client's people: a token confined to a client that can't change it.

        An API key is a client's systems, not its people, and reads its own
        agent's runs only (`_scoped_box`, `_scoped_run`).
        """
        if token is not None and token.allows(SCOPE_RUN_CREATE) and token.box is not None:
            return
        if token is not None and token.client is not None and not token.allows(SCOPE_FLEET_WRITE):
            raise HTTPException(status_code=403, detail="runs are the agency's to see")

    def _scoped_run(store: Any, token: Token | None, run_id: str) -> Any:
        """A run `token` may read, or 404, as `_scoped_box` is for a box."""
        run = store.get_run(run_id)
        if run is None or (
            token is not None
            and (
                admits_tenant(token, org=run.org, client=run.client) is not None
                or (token.box is not None and token.box != run.box_id)
            )
        ):
            raise HTTPException(status_code=404, detail=f"no run {run_id!r}")
        return run

    def _run_view(run: Any, cost: Any = None, approvals: Any = None) -> dict[str, Any]:
        from dataclasses import asdict

        view = asdict(run)
        if cost is not None:
            view["cost"] = asdict(cost)
        if approvals is not None:
            view["approvals"] = [asdict(a) for a in approvals]
            view["waiting_approval"] = any(a.status == "waiting" for a in approvals)
        return view

    @app.post("/api/boxes/{box_id}/runs", status_code=202)
    def create_run(box_id: str, body: dict[str, Any], token: Token | None = needs_run_write) -> Any:
        """Ask an agent for a piece of work: `{prompt, input?}`. It waits in
        `pending` until its agent is free; read it at `/api/runs/{id}`."""
        prompt = body.get("prompt")
        if not isinstance(prompt, str) or not prompt.strip():
            raise HTTPException(status_code=422, detail="a run needs a prompt: what to do")
        store = store_factory()
        try:
            box = _scoped_box(store, token, box_id)
            try:
                run = store.create_run(box.id, prompt, trigger_kind="api", input=body.get("input"))
            except UnknownEntityError as exc:
                raise HTTPException(status_code=404, detail=f"no box {box_id!r}") from exc
            except ValueError as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc
            store.add_event(
                "task",
                run.id,
                "created",
                {"trigger": "api", "by": token.subject if token else None},
            )
            return _run_view(run)
        finally:
            store.close()

    def _runs_since(raw: str | None) -> datetime | None:
        """`?since=`: a day (its midnight in UTC) or a moment that says its zone."""
        if raw is None:
            return None
        from flotta.usage_report import BadReport, parse_instant

        try:
            return parse_instant(raw, "since")
        except BadReport as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    def _runs_status(raw: str | None) -> list[str] | None:
        """`?status=`: the run statuses wanted, comma-separated."""
        if raw is None:
            return None
        wanted = [part.strip() for part in raw.split(",") if part.strip()]
        unknown = [s for s in wanted if s not in TASK_STATUSES]
        if unknown or not wanted:
            known = ", ".join(sorted(TASK_STATUSES))
            raise HTTPException(status_code=422, detail=f"status must be some of: {known}")
        return wanted

    @app.get("/api/boxes/{box_id}/runs")
    def list_box_runs(
        box_id: str,
        limit: int = 50,
        since: str | None = None,
        status: str | None = None,
        token: Token | None = needs_run_read,
    ) -> Any:
        """An agent's runs, newest first, each with what it cost; `?since=` the
        ones made from then on, `?status=failed,budget_blocked` those that
        ended so."""
        after = _runs_since(since)
        statuses = _runs_status(status)
        store = store_factory()
        try:
            box = _scoped_box(store, token, box_id)
            _run_reader(token)
            runs = store.list_runs(
                org=box.org, box=box.id, since=after, statuses=statuses, limit=min(limit, 200)
            )
            costs = store.run_costs(runs)
            return {"runs": [_run_view(r, costs[r.id]) for r in runs]}
        finally:
            store.close()

    @app.get("/api/clients/{client_id}/runs")
    def list_client_runs(
        client_id: str,
        limit: int = 50,
        since: str | None = None,
        status: str | None = None,
        token: Token | None = needs_read,
    ) -> Any:
        """A client's runs in the caller's org, newest first, each with what it
        cost; `?since=` the ones made from then on, `?status=` those that ended
        one of these ways."""
        org = (token.org if token else None) or DEFAULT_ORG
        if token is not None and token.client is not None and token.client != client_id:
            raise HTTPException(status_code=404, detail=f"no client {client_id!r}")
        _run_reader(token)
        after = _runs_since(since)
        statuses = _runs_status(status)
        store = store_factory()
        try:
            runs = store.list_runs(
                org=org, client=client_id, since=after, statuses=statuses, limit=min(limit, 200)
            )
            costs = store.run_costs(runs)
            return {"runs": [_run_view(r, costs[r.id]) for r in runs]}
        finally:
            store.close()

    @app.get("/api/runs/{run_id}")
    def get_run(run_id: str, token: Token | None = needs_run_read) -> Any:
        """One run: what started it, what it was given, its steps, its reply and its cost."""
        store = store_factory()
        try:
            run = _scoped_run(store, token, run_id)
            _run_reader(token)
            return _run_view(run, store.run_cost(run.id), store.run_approvals(run.id))
        finally:
            store.close()

    # A run's agent asked permission and waits for a person (`flotta.runner`).
    # FLOTTA-124's rules, as the door keeps them for a conversation: the
    # agency answers with any choice the box offered; a client's people only
    # where the agency allowed it for that agent, and never "always".

    @app.post("/api/runs/{run_id}/approval")
    def answer_run_approval(
        run_id: str, body: dict[str, Any], token: Token | None = needs_answer
    ) -> Any:
        """`{choice, approval?}`: answer what the run's agent is waiting on.

        `approval` names one when the agent asked more than one; without it, the
        one waiting. The run's delivery sends the choice to the box.
        """
        from dataclasses import asdict

        from flotta.approval_choices import CLIENT_APPROVAL_CHOICES
        from flotta.store import ApprovalClosedError

        choice = body.get("choice")
        store = store_factory()
        try:
            run = _scoped_run(store, token, run_id)
            box = _scoped_box(store, token, run.box_id)
            if run.status != "running":
                raise HTTPException(
                    status_code=409, detail=f"run {run_id!r} is {run.status}: nobody would hear it"
                )
            allowed: frozenset[str] | None = None
            if token is not None:
                if token.box is not None or subject_box(token.subject) is not None:
                    raise HTTPException(status_code=403, detail="an agent answers no approval")
                if token.client is not None:
                    if not store.clients_answer_approvals(box.id):
                        raise HTTPException(
                            status_code=403,
                            detail="the agency answers this agent's approvals",
                        )
                    allowed = CLIENT_APPROVAL_CHOICES
                elif not token.allows(SCOPE_FLEET_WRITE):
                    raise HTTPException(
                        status_code=403, detail=f"answering needs {SCOPE_FLEET_WRITE}"
                    )
            if run.trigger_kind == "chat":
                # A person's conversation: the door carries only these to the
                # box, so nothing else is taken (FLOTTA-166).
                from flotta.approval_choices import OUTSIDE_CHOICES

                allowed = frozenset(OUTSIDE_CHOICES) & (allowed or frozenset(OUTSIDE_CHOICES))
            waiting = [a for a in store.run_approvals(run.id) if a.status == "waiting"]
            named = body.get("approval")
            if named is not None:
                waiting = [a for a in waiting if a.id == named]
            if len(waiting) != 1:
                detail = (
                    "nothing is waiting for an answer"
                    if not waiting
                    else "more than one approval is waiting: name one with `approval`"
                )
                raise HTTPException(status_code=409, detail=detail)
            approval = waiting[0]
            if choice not in approval.choices or (allowed is not None and choice not in allowed):
                offered = [c for c in approval.choices if allowed is None or c in allowed]
                raise HTTPException(
                    status_code=422, detail=f"choose one of {offered}, got {choice!r}"
                )
            try:
                answered = store.answer_approval(
                    approval.id, choice, by=token.subject if token else "local"
                )
            except ApprovalClosedError as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            store.add_event(
                "task", run.id, "approval_answered", {"choice": choice, "by": answered.answered_by}
            )
            return asdict(answered)
        finally:
            store.close()

    @app.get("/api/approvals")
    def list_waiting_approvals(token: Token | None = needs_answer) -> Any:
        """The approvals waiting for a person that the caller may answer, oldest first.

        The agency's: every one in its org. A client's people: their client's,
        on the agents the agency lets them answer for.
        """

        if token is not None and (token.box is not None or subject_box(token.subject)):
            raise HTTPException(status_code=403, detail="an agent answers no approval")
        org = (token.org if token else None) or DEFAULT_ORG
        store = store_factory()
        try:
            if token is None or token.client is None:
                found = store.waiting_approvals(org=org)
            else:
                boxes = [
                    b.id
                    for b in _scoped_boxes(store, token)
                    if store.clients_answer_approvals(b.id)
                ]
                found = store.waiting_approvals(org=org, client=token.client, boxes=boxes)
            return {"approvals": [_approval_view(store, a) for a in found]}
        finally:
            store.close()

    def _approval_view(store: FleetStore, approval: Any) -> dict[str, Any]:
        """An approval, with where it was asked (FLOTTA-166): `kind` is `run`
        or `chat`; a chat's also has `session_id`, the conversation, and
        `whose`, the person in it, by name or email when they are a user."""
        from dataclasses import asdict

        view: dict[str, Any] = {**asdict(approval), "kind": "run"}
        trigger, given = store.asked_in(approval)
        if trigger == "chat":
            view["kind"] = "chat"
            view["session_id"] = chat_conversation(given)
            by = given.get("by")
            view["whose"] = _person(store, by)
        return view

    def _person(store: FleetStore, subject: Any) -> str | None:
        """A token's subject as a person's name or email, when it is a user's."""
        from flotta.auth import USER_SUBJECT_PREFIX

        if not isinstance(subject, str) or not subject.startswith(USER_SUBJECT_PREFIX):
            return None
        user = store.get_user(subject.removeprefix(USER_SUBJECT_PREFIX))
        if user is None:
            return None
        return user.name or user.email

    # -- triggers (M14.3) ------------------------------------------------------
    #
    # What starts an agent's runs on its own: schedules and webhooks. The agency's,
    # like runs, and a client's own systems' (a client's token with
    # `fleet:write`); a client's people see none. Checked in the store, where
    # every caller meets the same rules (`flotta.schedules.check`).

    def _scoped_trigger(store: Any, token: Token | None, trigger_id: str) -> Any:
        """A trigger `token` may act on, or 404, as `_scoped_box` is for a box."""
        trigger = store.get_trigger(trigger_id)
        if trigger is None or (
            token is not None
            and (
                admits_tenant(token, org=trigger.org, client=trigger.client) is not None
                or (token.box is not None and token.box != trigger.box_id)
            )
        ):
            raise HTTPException(status_code=404, detail=f"no trigger {trigger_id!r}")
        return trigger

    def _trigger_view(trigger: Any) -> dict[str, Any]:
        """A trigger, with its next three times so the web app can say "next: Mon 08:00"."""
        from dataclasses import asdict

        from flotta import schedules

        view = asdict(trigger)
        upcoming: list[str] = []
        if trigger.enabled and trigger.kind == "schedule":
            cron = schedules.parse(trigger.cron)
            now = datetime.now(UTC)
            upcoming = [t.isoformat() for t in schedules.next_fires(cron, trigger.tz, now, 3)]
        view["next"] = upcoming
        if trigger.kind == "webhook":
            view["url"] = _hook_url(trigger.id)
        return view

    def _hook_url(trigger_id: str) -> str:
        """Where a webhook is called. `$FLOTTA_HOOKS_URL` once `hooks.flotta.dev`
        points here; until then the control plane's own `$FLOTTA_CONTROL_URL`."""
        base = (
            os.environ.get("FLOTTA_HOOKS_URL") or os.environ.get("FLOTTA_CONTROL_URL") or ""
        ).strip()
        return f"{base.rstrip('/')}/hooks/{trigger_id}"

    def _with_secret(store: Any, trigger: Any) -> dict[str, Any]:
        """A webhook's view and its secret: answered once, when it is made or changed."""
        from flotta.auth import trigger_secret

        view = _trigger_view(trigger)
        view["secret"] = trigger_secret(key, trigger.id, store.trigger_nonce(trigger.id) or "")
        return view

    @app.post("/api/boxes/{box_id}/triggers", status_code=201)
    def create_trigger(box_id: str, body: dict[str, Any], token: Token | None = needs_write) -> Any:
        """A trigger on an agent: a schedule, `{cron, tz, prompt, enabled?}`, or
        a webhook, `{kind: "webhook", prompt, enabled?}`.

        **A webhook answers with its `url` and its `secret`, this once.** The
        secret is not stored and cannot be read again; a new one is
        `POST /api/triggers/{id}/secret`. `docs/WEBHOOKS.md` says how to call it.
        """
        kind = body.get("kind", "schedule")
        store = store_factory()
        try:
            box = _scoped_box(store, token, box_id)
            try:
                trigger = store.create_trigger(
                    box.id,
                    kind=str(kind),
                    cron=str(body.get("cron") or "") if kind == "schedule" else body.get("cron"),
                    tz=str(body.get("tz") or "UTC") if kind == "schedule" else body.get("tz"),
                    prompt=body.get("prompt") or "",
                    enabled=bool(body.get("enabled", True)),
                    created_by=token.subject if token else None,
                )
            except UnknownEntityError as exc:
                raise HTTPException(status_code=404, detail=f"no box {box_id!r}") from exc
            except ValueError as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc
            if trigger.kind == "webhook":
                return _with_secret(store, trigger)
            return _trigger_view(trigger)
        finally:
            store.close()

    @app.post("/api/triggers/{trigger_id}/secret")
    def rotate_trigger_secret(trigger_id: str, token: Token | None = needs_write) -> Any:
        """A webhook's new secret, answered this once. The old one stops at once."""
        store = store_factory()
        try:
            trigger = _scoped_trigger(store, token, trigger_id)
            try:
                store.rotate_trigger_secret(trigger.id)
            except ValueError as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc
            return _with_secret(store, trigger)
        finally:
            store.close()

    @app.get("/api/boxes/{box_id}/triggers")
    def list_box_triggers(box_id: str, token: Token | None = needs_read) -> Any:
        """An agent's triggers, oldest first, each with when it fires next."""
        store = store_factory()
        try:
            box = _scoped_box(store, token, box_id)
            _run_reader(token)
            return {"triggers": [_trigger_view(t) for t in store.list_triggers(box.id)]}
        finally:
            store.close()

    @app.patch("/api/triggers/{trigger_id}")
    def update_trigger(
        trigger_id: str, body: dict[str, Any], token: Token | None = needs_write
    ) -> Any:
        """Change a trigger: any of `{cron, tz, prompt, enabled}`; a webhook's
        `{prompt, enabled}`."""
        store = store_factory()
        try:
            trigger = _scoped_trigger(store, token, trigger_id)
            enabled = body.get("enabled")
            try:
                changed = store.update_trigger(
                    trigger.id,
                    cron=body.get("cron"),
                    tz=body.get("tz"),
                    prompt=body.get("prompt"),
                    enabled=None if enabled is None else bool(enabled),
                )
            except ValueError as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc
            return _trigger_view(changed)
        finally:
            store.close()

    @app.delete("/api/triggers/{trigger_id}", status_code=204)
    def delete_trigger(trigger_id: str, token: Token | None = needs_write) -> None:
        """Remove a trigger. The runs it made stay. A webhook's URL is then
        unknown, like any other."""
        store = store_factory()
        try:
            trigger = _scoped_trigger(store, token, trigger_id)
            store.delete_trigger(trigger.id)
        finally:
            store.close()

    # -- API keys (M14.5) --------------------------------------------------
    #
    # What a client's own systems hold to start one agent's runs and read them
    # back: `Authorization: Bearer flotta_key_…` on the three run routes, and
    # nothing else (`require`, `key_token`). The agency's to make and revoke,
    # with `fleet:write`; a key cannot make another.

    def _scoped_key(store: Any, token: Token | None, key_id: str) -> Any:
        """An API key `token` may act on, or 404, as `_scoped_box` is for a box."""
        found = store.get_api_key(key_id)
        if found is None or (
            token is not None
            and (
                admits_tenant(token, org=found.org, client=found.client) is not None
                or (token.box is not None and token.box != found.box_id)
            )
        ):
            raise HTTPException(status_code=404, detail=f"no API key {key_id!r}")
        return found

    def _key_view(found: Any) -> dict[str, Any]:
        from dataclasses import asdict

        return asdict(found)

    @app.post("/api/boxes/{box_id}/keys", status_code=201)
    def create_api_key(box_id: str, body: dict[str, Any], token: Token | None = needs_write) -> Any:
        """A key for this agent: `{name}`. **The answer's `key` is shown this
        once**; the store keeps only its hash. `docs/API_KEYS.md` says how to use it."""
        store = store_factory()
        try:
            box = _scoped_box(store, token, box_id)
            try:
                found, secret = store.create_api_key(
                    box.id,
                    name=str(body.get("name") or ""),
                    created_by=token.subject if token else None,
                )
            except UnknownEntityError as exc:
                raise HTTPException(status_code=404, detail=f"no box {box_id!r}") from exc
            except ValueError as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc
            store.add_event(
                "box",
                box.id,
                "api_key_created",
                {"key": found.id, "name": found.name, "by": token.subject if token else None},
            )
            return {**_key_view(found), "key": secret}
        finally:
            store.close()

    @app.get("/api/boxes/{box_id}/keys")
    def list_api_keys(box_id: str, token: Token | None = needs_write) -> Any:
        """An agent's keys, newest first, revoked ones too. Never the keys themselves."""
        store = store_factory()
        try:
            box = _scoped_box(store, token, box_id)
            return {"keys": [_key_view(k) for k in store.list_api_keys(box.id)]}
        finally:
            store.close()

    @app.delete("/api/keys/{key_id}", status_code=204)
    def revoke_api_key(key_id: str, token: Token | None = needs_write) -> None:
        """Revoke a key: its next request is refused. The runs it started stay."""
        store = store_factory()
        try:
            found = _scoped_key(store, token, key_id)
            store.revoke_api_key(found.id)
            store.add_event(
                "box",
                found.box_id,
                "api_key_revoked",
                {"key": found.id, "name": found.name, "by": token.subject if token else None},
            )
        finally:
            store.close()

    # -- webhooks (M14.4) --------------------------------------------------
    #
    # `POST /hooks/{trigger_id}`, called by a client's CRM or form or Zap, which
    # holds the trigger's secret and no token. The trigger id alone is the
    # address, not `/<org>/<client>/<trigger>` as the spec first had it: the id
    # names all three already, and a URL is stored in a client's CRM, pasted
    # into tickets and printed in logs, so it should say nothing about whose
    # agent is behind it. `flotta.hooks` has who may call it and how.

    def _hooked_trigger(store: Any, trigger_id: str, headers: Any, raw: bytes) -> Any:
        """The webhook `trigger_id`, if the request proves it holds its secret
        and it is on; else None, whichever of those it was.

        This is the webhook's tenant check: the secret is the credential, and
        it was given only to whoever the trigger's agency gave it. One answer
        for "no such trigger", "wrong secret" and "turned off", so a caller
        without the secret learns nothing about which ids exist.
        """
        from flotta import hooks
        from flotta.auth import trigger_secret

        trigger = store.get_trigger(trigger_id)
        nonce = store.trigger_nonce(trigger_id)
        # The same reads, and a secret checked, even when there is no trigger,
        # so the answer takes about the same time either way.
        secret = trigger_secret(key, trigger_id, nonce or "no-such-trigger")
        proved = hooks.authenticated(secret, headers, raw, now=time.time())
        if trigger is None or nonce is None or not proved or not trigger.enabled:
            return None
        if trigger.kind != "webhook":
            return None
        return trigger

    @app.post("/hooks/{trigger_id}")
    async def receive_hook(trigger_id: str, request: Request, wait: int = 0) -> Any:
        """A client's system calls an agent: a JSON body starts a run.

        `202 {run_id, status}`, or with `?wait=N` (at most 30) the connection is
        held until the run ends and the answer is `200` with its `output`, or
        `202` if it has not ended by then. 413 for a body over 64 KB and 415
        for one that is not JSON, both before anything about the trigger is
        read. A missing, wrong or stale secret, an unknown trigger and one that
        is off all answer the same 404. 429 past 30 calls a minute.
        """
        import json

        from fastapi.responses import JSONResponse

        from flotta import hooks
        from flotta.store import TASK_TERMINAL

        not_found = HTTPException(status_code=404, detail="no such webhook")
        declared = request.headers.get("content-length")
        if declared and declared.isdigit() and int(declared) > hooks.MAX_BODY:
            raise HTTPException(status_code=413, detail="a webhook's body is at most 64 KB")
        raw = b""
        async for chunk in request.stream():
            raw += chunk
            if len(raw) > hooks.MAX_BODY:
                raise HTTPException(status_code=413, detail="a webhook's body is at most 64 KB")
        kind = request.headers.get("content-type", "").split(";")[0].strip().lower()
        if kind != "application/json" and not kind.endswith("+json"):
            raise HTTPException(status_code=415, detail="a webhook's body is JSON")
        try:
            body = json.loads(raw or b"null")
        except ValueError as exc:
            raise HTTPException(status_code=415, detail="a webhook's body is JSON") from exc

        store = store_factory()
        try:
            trigger = _hooked_trigger(store, trigger_id, request.headers, raw)
            if trigger is None:
                raise not_found
            if not hooks_per_trigger.hit(trigger.id):
                raise HTTPException(
                    status_code=429, detail="too many calls to this webhook; try in a minute"
                )
            try:
                run = store.fire_webhook(
                    trigger.id,
                    prompt=hooks.render(trigger.prompt, body),
                    input=hooks.run_input(raw, body),
                )
            except UnknownEntityError as exc:
                raise not_found from exc
            if run is None:
                raise not_found
            store.add_event("task", run.id, "created", {"trigger": "webhook", "by": trigger.id})
        finally:
            store.close()

        deadline = time.monotonic() + max(0, min(wait, hooks.MAX_WAIT_S))
        while run.status not in TASK_TERMINAL and time.monotonic() < deadline:
            await asyncio.sleep(0.5)
            store = store_factory()
            try:
                run = store.trigger_run(trigger.id, run.id) or run
            finally:
                store.close()
        answer: dict[str, Any] = {"run_id": run.id, "status": run.status}
        if wait > 0 and run.status in TASK_TERMINAL:
            return JSONResponse({**answer, "output": run.output}, status_code=200)
        return JSONResponse(answer, status_code=202)

    async def _deliver_run(run: Any, box: Any, talk: Any) -> str:
        """The default delivery: through the front door, with a token for this
        run's agent alone (`runner.run_token`), alive for the conversation."""
        from flotta import runner
        from flotta.runs import RUN_TIMEOUT_S

        assert key is not None
        token = runner.run_token(run, box, key=key, ttl_s=int(RUN_TIMEOUT_S) + 60)
        return await runner.through_door(
            box,
            runner.message(run),
            token=token,
            talk=talk,
            timeout_s=RUN_TIMEOUT_S,
            title=f"Run {run.id}",
        )

    carry_run = deliver_run if deliver_run is not None else (_deliver_run if key else None)

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

        **What this does and does not enforce depends on the source.** Flotta
        refuses a credential for a repository the box was not granted either way.

        - On a GitHub account its agency connected (FLOTTA-22), the credential
          is a 1-hour token for that one repository. GitHub enforces that, so a
          box that extracted it reaches nothing else.
        - On `$FLOTTA_GITHUB_TOKEN`, for the operator's agents only, it is the
          whole fleet token: a box that extracted it could use it beyond its
          grants. That is policy enforced here, not by GitHub. Stated plainly
          because a soft boundary that reads as a hard one is worse than no
          boundary at all.

        **A box token may only ask about its own box.** Scopes say what a token
        may do, never to which box, and that gap became load-bearing the moment
        a `git:credential` token started living on a machine whose agent has
        root. Without this check one box's token would reach every other box's
        grants, which is precisely the containment the grants exist to provide.
        """
        store = store_factory()
        try:
            box = _scoped_box(store, token, box_id)

            # Before anything else: is this token allowed to speak for this
            # box? A non-box subject (an operator) is unrestricted — see
            # `auth.BOX_SUBJECT_PREFIX`.
            confine_to_box(token, box, "mint credentials for")
            repo = str(body.get("repo") or "").strip()
            if not repo:
                raise HTTPException(status_code=422, detail="which repository?")
            password, source, repo = _repo_token(store, box, repo)

            store.add_event("box", box.id, "git_credential", {"repo": repo, "source": source})
            # The username is ignored by GitHub when the password is a token;
            # `x-access-token` is the convention its own docs use.
            return {"username": "x-access-token", "password": password, "repo": repo}
        finally:
            store.close()

    # -- pull requests (FLOTTA-171, FLOTTA-172) ---------------------------------

    needs_box_or_read = Depends(require(SCOPE_FLEET_READ, SCOPE_GIT_CREDENTIAL, any_of=True))

    def _scoped_pull(store: Any, token: Token | None, pull_id: str) -> Any:
        """A pull request `token` may act on, or 404, as `_scoped_box` is for a box."""
        pr = store.get_pull_request(pull_id)
        if pr is None or (
            token is not None
            and (
                admits_tenant(token, org=pr.org, client=pr.client) is not None
                or (token.box is not None and token.box != pr.box_id)
            )
        ):
            raise HTTPException(status_code=404, detail=f"no pull request {pull_id!r}")
        return pr

    def _policy_view(policy: Any) -> dict[str, Any]:
        return {"may_merge": policy.may_merge, "follow_reviews": policy.follow_reviews}

    @app.post("/api/boxes/{box_id}/pull-requests")
    def report_pull_request(
        box_id: str, body: dict[str, Any], token: Token | None = needs_git
    ) -> Any:
        """A pull request this agent opened (`flotta-pr report`, which its `gh`
        runs after `gh pr create`). `{"url": "https://github.com/o/r/pull/n"}`.

        The box says which PR; GitHub says what it is. It is read with a token
        for a repository this box is granted, so a box can report only a real
        PR, in a repository it was given. Reporting the same PR again answers
        it as it is (`created: false`).
        """
        from flotta import pulls

        try:
            repo, number = pulls.parse_url(str(body.get("url") or ""))
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        store = store_factory()
        try:
            box = _scoped_box(store, token, box_id)
            confine_to_box(token, box, "report pull requests for")
            github, _, repo = _repo_token(store, box, repo)
            try:
                fresh = pulls.read_pull(_github_fetch, github, repo, number)
            except pulls.PullError as exc:
                raise HTTPException(status_code=exc.status, detail=exc.detail) from exc
            try:
                pr, created = store.record_pull_request(box.id, repo=repo, number=number, **fresh)
            except ValueError as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            except UnknownEntityError as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            if created:
                store.add_event(
                    "box",
                    box.id,
                    "pull_request_opened",
                    {"repo": repo, "number": number, "title": pr.title},
                )
            return {"pull_request": pulls.view(store, pr), "created": created}
        finally:
            store.close()

    def _clients_person(token: Token | None) -> bool:
        """Whether the caller is one of a client's people or systems, who see a
        request's or PR's outcome and not the agency's notes on it (FLOTTA-174).
        An agent is not: it reads its own requests, reasons and all."""
        return (
            token is not None
            and token.client is not None
            and token.box is None
            and subject_box(token.subject) is None
        )

    @app.get("/api/boxes/{box_id}/pull-requests")
    def box_pull_requests(box_id: str, token: Token | None = needs_box_or_read) -> Any:
        """This agent's pull requests: every open one, then the latest others."""
        from flotta import pulls

        store = store_factory()
        try:
            box = _scoped_box(store, token, box_id)
            confine_to_box(token, box, "read the pull requests of")
            found = store.pull_requests(org=box.org, box=box.id)
            mine = _clients_person(token)
            return {"pull_requests": [pulls.view(store, pr, for_client=mine) for pr in found]}
        finally:
            store.close()

    @app.get("/api/pull-requests")
    def list_pull_requests(token: Token | None = needs_read) -> Any:
        """Every pull request the caller's agents opened: the org's for the
        agency, one client's for its people. Open first, newest first."""
        from flotta import pulls

        if token is not None and (token.box is not None or subject_box(token.subject)):
            raise HTTPException(status_code=403, detail="an agent reads its own, on its own box")
        org = (token.org if token else None) or DEFAULT_ORG
        store = store_factory()
        try:
            found = store.pull_requests(org=org, client=token.client if token else None)
            mine = _clients_person(token)
            return {"pull_requests": [pulls.view(store, pr, for_client=mine) for pr in found]}
        finally:
            store.close()

    @app.post("/api/pull-requests/{pull_id}/merge")
    def merge_pull_request(
        pull_id: str, body: dict[str, Any] | None = None, token: Token | None = needs_write
    ) -> Any:
        """Merge an agent's pull request from Flotta, as the agency.

        With the same token the agent would have had for the repository, so the
        agency can merge only what its agent was granted. `{"method": "squash"}`
        (the default), `"merge"` or `"rebase"`; GitHub's own refusal (a branch
        rule, a conflict) is the 409's words.
        """
        from flotta import pulls

        method = str((body or {}).get("method") or "squash")
        store = store_factory()
        try:
            pr = _scoped_pull(store, token, pull_id)
            if pr.state != "open":
                raise HTTPException(status_code=409, detail=f"{pr.repo}#{pr.number} is {pr.state}")
            box = _scoped_box(store, token, pr.box_id)
            github, _, _ = _repo_token(store, box, pr.repo)
            try:
                pulls.merge(_github_fetch, github, pr.repo, pr.number, method)
            except pulls.PullError as exc:
                raise HTTPException(status_code=exc.status, detail=exc.detail) from exc
            who = (_person(store, token.subject) if token is not None else None) or "the operator"
            # GitHub said it merged. Who it says merged it, and when, is read
            # back when it can be; if not, now is close enough until the sweep.
            changes: dict[str, Any] = {
                "merged_by_person": who,
                "state": "merged",
                "merged_at": datetime.now(UTC).isoformat(),
            }
            with contextlib.suppress(pulls.PullError):
                fresh = pulls.read_pull(_github_fetch, github, pr.repo, pr.number)
                if fresh["state"] == "merged":
                    changes.update(
                        merged_at=fresh["merged_at"],
                        merged_by=fresh["merged_by"],
                        merge_sha=fresh["merge_sha"],
                    )
            pr = store.update_pull_request(pr.id, **changes)
            store.add_event(
                "box",
                pr.box_id,
                "pull_request_merged",
                {"repo": pr.repo, "number": pr.number, "by": who, "method": method, "in": "flotta"},
            )
            return {"pull_request": pulls.view(store, pr)}
        finally:
            store.close()

    @app.get("/api/boxes/{box_id}/pr-policy")
    def get_pr_policy(box_id: str, token: Token | None = needs_box_or_read) -> Any:
        """Whether this agent may merge its own pull requests, and follows their
        reviews. Its own box asks before `gh pr merge` (`flotta-pr may-merge`)."""
        store = store_factory()
        try:
            box = _scoped_box(store, token, box_id)
            confine_to_box(token, box, "read the pull-request policy of")
            return _policy_view(store.pr_policy(box.id))
        finally:
            store.close()

    @app.put("/api/boxes/{box_id}/pr-policy")
    def put_pr_policy(box_id: str, body: dict[str, Any], token: Token | None = needs_write) -> Any:
        """Change either or both: `{"may_merge": bool, "follow_reviews": bool}`.

        "May merge" is enforced by the box's `gh`, which asks before it merges:
        a guard against an agent merging by mistake or because someone asked it
        to, not a wall, since the agent has root and its token may merge.
        """
        from flotta.store import PrPolicy

        unknown = set(body) - {"may_merge", "follow_reviews"}
        if unknown:
            raise HTTPException(status_code=422, detail=f"no setting {sorted(unknown)[0]!r}")
        for key, value in body.items():
            if not isinstance(value, bool):
                raise HTTPException(status_code=422, detail=f"{key} is true or false")
        store = store_factory()
        try:
            box = _scoped_box(store, token, box_id)
            current = store.pr_policy(box.id)
            policy = store.set_pr_policy(
                box.id,
                PrPolicy(
                    may_merge=body.get("may_merge", current.may_merge),
                    follow_reviews=body.get("follow_reviews", current.follow_reviews),
                ),
            )
            store.add_event(
                "box",
                box.id,
                "pr_policy_set",
                {
                    **_policy_view(policy),
                    "by": token.subject if token is not None else "operator",
                },
            )
            return _policy_view(policy)
        finally:
            store.close()

    # -- requests (FLOTTA-173) ---------------------------------------------------

    needs_peer_or_read = Depends(require(SCOPE_FLEET_READ, SCOPE_BOX_PEER, any_of=True))

    def _scoped_request(store: Any, token: Token | None, request_id: str) -> Any:
        """A request `token` may act on, or 404, as `_scoped_box` is for a box."""
        found = store.get_request(request_id)
        if found is None or (
            token is not None
            and (
                admits_tenant(token, org=found.org, client=found.client) is not None
                or (token.box is not None and token.box != found.box_id)
            )
        ):
            raise HTTPException(status_code=404, detail=f"no request {request_id!r}")
        return found

    def _request_policy_view(policy: Any) -> dict[str, Any]:
        from flotta.judge import COMPLEXITY_NAMES

        level = policy.auto_level
        return {
            "auto_level": level,
            "auto_level_name": COMPLEXITY_NAMES[level] if level >= 0 else None,
            "min_confidence": policy.min_confidence,
            "levels": list(COMPLEXITY_NAMES),
        }

    @app.post("/api/boxes/{box_id}/requests", status_code=201)
    def file_request(box_id: str, body: dict[str, Any], token: Token | None = needs_peer) -> Any:
        """A piece of work someone in a conversation asked this agent for.
        `{"title": "…", "details": "…"}`. Its own box files it (`flotta-request`).

        Judged by Jev, then decided against the agent's level: `starts` says
        whether the agent may go ahead now, `why` why, in words the agent can
        pass on. Anything that cannot be judged waits for a person.
        """
        from flotta import judge as jev
        from flotta import work_requests

        title, details = body.get("title"), body.get("details", "")
        if not isinstance(title, str) or not isinstance(details, str):
            raise HTTPException(status_code=422, detail="send a title, and details as text")
        store = store_factory()
        try:
            box = _scoped_box(store, token, box_id)
            confine_to_box(token, box, "file requests for")
            policy = store.request_policy(box.id)
            verdict, error = None, None
            try:
                verdict = (judge_request or jev.judge)(title, details)
            except jev.JudgeError as exc:
                error = str(exc)
            decision = jev.decide(
                verdict, auto_level=policy.auto_level, min_confidence=policy.min_confidence
            )
            try:
                filed = store.file_request(
                    box.id,
                    title=title,
                    details=details,
                    verdict=verdict,
                    judge_error=error,
                    within=verdict.within(policy.auto_level) if verdict else None,
                    approved=decision.starts,
                    why=decision.why,
                )
            except ValueError as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc
            except UnknownEntityError as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            store.add_event(
                "box",
                box.id,
                "request_filed",
                {
                    "request": filed.id,
                    "title": filed.title[:EVENT_TEXT_CHARS],
                    "starts": decision.starts,
                },
            )
            return {
                "request": work_requests.view(store, filed),
                "starts": decision.starts,
                "why": decision.why,
            }
        finally:
            store.close()

    @app.get("/api/boxes/{box_id}/requests")
    def box_requests(box_id: str, token: Token | None = needs_peer_or_read) -> Any:
        """This agent's requests: every waiting one, then the latest decided."""
        from flotta import work_requests

        store = store_factory()
        try:
            box = _scoped_box(store, token, box_id)
            confine_to_box(token, box, "read the requests of")
            found = store.list_requests(org=box.org, box=box.id)
            mine = _clients_person(token)
            return {"requests": [work_requests.view(store, r, for_client=mine) for r in found]}
        finally:
            store.close()

    @app.get("/api/requests")
    def list_requests(token: Token | None = needs_read) -> Any:
        """Every request the caller's agents filed: the org's for the agency, one
        client's for its people. Waiting first, oldest first."""
        from flotta import work_requests

        if token is not None and (token.box is not None or subject_box(token.subject)):
            raise HTTPException(status_code=403, detail="an agent reads its own, on its own box")
        org = (token.org if token else None) or DEFAULT_ORG
        store = store_factory()
        try:
            found = store.list_requests(org=org, client=token.client if token else None)
            mine = _clients_person(token)
            return {"requests": [work_requests.view(store, r, for_client=mine) for r in found]}
        finally:
            store.close()

    @app.post("/api/requests/{request_id}/decision")
    def decide_request(
        request_id: str, body: dict[str, Any], token: Token | None = needs_write
    ) -> Any:
        """The agency's answer to a waiting request: `{"approve": true|false, "note": "…"}`.

        Approved, the work becomes a run on the agent, with the note; declined,
        the note is kept for the agent to pass on when the person asks.
        """
        from flotta import work_requests

        approve, note = body.get("approve"), body.get("note")
        if not isinstance(approve, bool):
            raise HTTPException(status_code=422, detail='send {"approve": true} or false')
        if note is not None and not isinstance(note, str):
            raise HTTPException(status_code=422, detail="a note is text")
        store = store_factory()
        try:
            found = _scoped_request(store, token, request_id)
            who = (_person(store, token.subject) if token is not None else None) or (
                token.subject if token is not None else "the operator"
            )
            try:
                decided = store.decide_request(found.id, approve=approve, by=who, note=note)
            except ValueError as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            if approve:
                try:
                    run = store.create_run(
                        decided.box_id,
                        work_requests.work_prompt(decided, note=decided.note),
                        trigger_kind="request",
                        trigger_id=decided.id,
                        input=work_requests.run_input(decided),
                    )
                    decided = store.set_request_work(decided.id, run.id)
                except UnknownEntityError:
                    pass  # its agent is gone: the decision stands, with nothing to run it
            store.add_event(
                "box",
                decided.box_id,
                "request_approved" if approve else "request_declined",
                {"request": decided.id, "by": who, "run": decided.work_run_id},
            )
            return {"request": work_requests.view(store, decided)}
        finally:
            store.close()

    # -- settling what failed (FLOTTA-201/202) ---------------------------------
    #
    # The agency's: a person who can change its agents. Not a client's people
    # or systems, and not an API key: they can't see runs (D21), so they can't
    # say one has been dealt with either.

    def _settler(token: Token | None) -> str:
        """Who is resolving or rerunning, by name, or 403."""
        if token is not None and (token.client is not None or token.box is not None):
            raise HTTPException(status_code=403, detail="that is the agency's to settle")
        return (token.subject if token is not None else None) or "the operator"

    def _note_of(body: dict[str, Any]) -> str | None:
        note = body.get("note")
        if note is not None and not isinstance(note, str):
            raise HTTPException(status_code=422, detail="a note is text")
        return note

    @app.post("/api/requests/{request_id}/retry")
    def retry_request(request_id: str, token: Token | None = needs_write) -> Any:
        """Run an approved request's work again, as the same request: once its
        last attempt failed, was held or was stopped. The request then follows
        the new run, in its conversation and on Home."""
        from flotta import work_requests

        subject = _settler(token)
        store = store_factory()
        try:
            found = _scoped_request(store, token, request_id)
            who = _person(store, subject) or subject
            try:
                retried = store.retry_request(
                    found.id,
                    prompt=work_requests.work_prompt(found, note=found.note),
                    input=work_requests.run_input(found),
                    by=who,
                )
            except ValueError as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            except UnknownEntityError as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            store.add_event(
                "box",
                retried.box_id,
                "request_retried",
                {"request": retried.id, "by": who, "run": retried.work_run_id},
            )
            return {"request": work_requests.view(store, retried)}
        finally:
            store.close()

    @app.post("/api/requests/{request_id}/resolve")
    def resolve_request(
        request_id: str, body: dict[str, Any], token: Token | None = needs_write
    ) -> Any:
        """`{"note": "…"}`: an approved request whose work failed was settled
        some other way. Its card in the conversation says so, with the note."""
        from flotta import work_requests

        subject = _settler(token)
        note = _note_of(body)
        store = store_factory()
        try:
            found = _scoped_request(store, token, request_id)
            who = _person(store, subject) or subject
            try:
                resolved = store.resolve_request(found.id, by=who, note=note)
            except ValueError as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            store.add_event(
                "box", resolved.box_id, "request_resolved", {"request": resolved.id, "by": who}
            )
            return {"request": work_requests.view(store, resolved)}
        finally:
            store.close()

    @app.post("/api/runs/{run_id}/resolve")
    def resolve_run(run_id: str, body: dict[str, Any], token: Token | None = needs_write) -> Any:
        """`{"note": "…"}`: a run that failed, was held or was stopped has been
        dealt with. Home stops asking about it; a request it was the work of is
        resolved with it."""
        subject = _settler(token)
        note = _note_of(body)
        store = store_factory()
        try:
            run = _scoped_run(store, token, run_id)
            who = _person(store, subject) or subject
            try:
                resolved = store.resolve_run(run.id, by=who, note=note)
            except ValueError as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            store.add_event("task", resolved.id, "resolved", {"by": who})
            return _run_view(resolved)
        finally:
            store.close()

    @app.get("/api/boxes/{box_id}/request-policy")
    def get_request_policy(box_id: str, token: Token | None = needs_peer_or_read) -> Any:
        """The most complex work this agent may start without the agency's OK."""
        store = store_factory()
        try:
            box = _scoped_box(store, token, box_id)
            confine_to_box(token, box, "read the request policy of")
            return _request_policy_view(store.request_policy(box.id))
        finally:
            store.close()

    @app.put("/api/boxes/{box_id}/request-policy")
    def put_request_policy(
        box_id: str, body: dict[str, Any], token: Token | None = needs_write
    ) -> Any:
        """`{"auto_level": -1..4, "min_confidence": 0.5..0.99}`: -1 is "always ask"."""
        from flotta.store import RequestPolicy

        unknown = set(body) - {"auto_level", "min_confidence"}
        if unknown:
            raise HTTPException(status_code=422, detail=f"no setting {sorted(unknown)[0]!r}")
        level, sure = body.get("auto_level"), body.get("min_confidence")
        if level is not None and (isinstance(level, bool) or not isinstance(level, int)):
            raise HTTPException(status_code=422, detail="auto_level is a whole number")
        if sure is not None and (isinstance(sure, bool) or not isinstance(sure, int | float)):
            raise HTTPException(status_code=422, detail="min_confidence is a number")
        store = store_factory()
        try:
            box = _scoped_box(store, token, box_id)
            current = store.request_policy(box.id)
            try:
                policy = store.set_request_policy(
                    box.id,
                    RequestPolicy(
                        auto_level=current.auto_level if level is None else level,
                        min_confidence=current.min_confidence if sure is None else float(sure),
                    ),
                )
            except ValueError as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc
            store.add_event(
                "box",
                box.id,
                "request_policy_set",
                {
                    "auto_level": policy.auto_level,
                    "min_confidence": policy.min_confidence,
                    "by": token.subject if token is not None else "operator",
                },
            )
            return _request_policy_view(policy)
        finally:
            store.close()

    @app.get("/api/boxes/{box_id}/github/repositories")
    def github_repositories(
        box_id: str, suggest: bool = True, token: Token | None = needs_write
    ) -> Any:
        """What could be granted to this agent, to suggest while granting (FLOTTA-22).

        Asks GitHub, per connected account, so a person picks a name rather than
        remembering one. Only a suggestion: `problems` says which accounts could
        not be asked, and a name missing here can still be typed and checked.
        `suggest=false` skips GitHub, for a page redrawing itself on a timer.

        Each account's list is kept briefly (FLOTTA-193,
        `GitHubApp.suggested_repositories`): a repeat open answers from it, and
        one more than a minute later answers from it while a refresh runs.
        Accounts that do have to wait for GitHub are asked side by side rather
        than one after another.
        """
        from flotta.github_app import TIMING_LOG, GitHubAppError

        store = store_factory()
        try:
            box = _operated_box(store, token, box_id)
            connected = store.github_installations_for(box.org, box.client)
        finally:
            store.close()
        app_ = _github_app_or_none()
        names: list[str] = []
        truncated = False
        problems: list[str] = []
        asked = connected if suggest else []
        started = time.perf_counter()
        sources: list[str] = []
        if app_ is None:
            problems += [installation.account for installation in asked]
        else:

            def suggest_from(installation: Any) -> Any:
                try:
                    return app_.suggested_repositories(installation.installation_id)
                except GitHubAppError:
                    return None

            if len(asked) > 1:
                from concurrent.futures import ThreadPoolExecutor

                with ThreadPoolExecutor(max_workers=min(len(asked), 4)) as pool:
                    answers = list(pool.map(suggest_from, asked))
            else:
                answers = [suggest_from(installation) for installation in asked]
            for installation, listing in zip(asked, answers, strict=True):
                if listing is None:
                    problems.append(installation.account)
                    continue
                names += listing.names
                truncated = truncated or listing.more
                sources.append(listing.source)
        if asked:
            # One line per page: how many accounts were asked, and whether each
            # answer was kept, stale or GitHub's (FLOTTA-180, FLOTTA-193).
            logging.getLogger(TIMING_LOG).info(
                "github repositories for %s: %d account(s) asked in %.0f ms (%s), %d problem(s)",
                box.id,
                len(asked),
                (time.perf_counter() - started) * 1000,
                ", ".join(sources) or "no answers",
                len(problems),
            )
        return {
            "box_id": box.id,
            "available": app_ is not None,
            "connected": [i.account for i in connected],
            "repositories": sorted(set(names)),
            "truncated": truncated,
            "problems": problems,
        }

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

    @app.get("/api/boxes/{box_id}/client-conversations")
    def client_conversations(box_id: str, token: Token | None = needs_route_or_write) -> Any:
        """Which of this agent's sessions are its client's (FLOTTA-126).

        The front door decides with this what a ticket may open. The agency's own
        people read it too, to open a client's conversation (FLOTTA-129): they
        hold the whole agent already. A client's people don't: they would learn
        the ids of sessions they may not open, and the door answers them.
        """
        store = store_factory()
        try:
            box = _routable_box(store, token, box_id)
            return {
                "conversations": [
                    {"id": session_id, "started_at": datetime.fromisoformat(at).timestamp()}
                    for session_id, at in store.client_conversations(box.id)
                ]
            }
        finally:
            store.close()

    @app.post("/api/boxes/{box_id}/client-conversations", status_code=201)
    def add_client_conversation(
        box_id: str, body: dict[str, Any], token: Token | None = needs_fleet_route
    ) -> Any:
        """A session the door just saw a client make."""
        session_id = body.get("session_id")
        if not isinstance(session_id, str) or not 0 < len(session_id) <= 200:
            raise HTTPException(status_code=422, detail="session_id is the session's stored id")
        store = store_factory()
        try:
            box = _routable_box(store, token, box_id)
            store.add_client_conversation(box.id, session_id)
            return {"id": session_id}
        finally:
            store.close()

    # -- a conversation's turns, as runs (M14.6) ----------------------------
    #
    # The door is the one process that sees every frame of a person's
    # conversation, so it records each turn as a `chat` run: opened when the
    # agent starts answering, ended with its steps when it has answered. Both
    # with the door's `fleet:route` token, and both best effort on the door's
    # side (`flotta.door.turns`), so a slow control plane costs a conversation
    # nothing. A run per turn, not per conversation: see `door/turns.py`.

    def _door_time(value: Any, name: str) -> datetime | None:
        """A time the door heard something at, or None if it sent none.

        The door sends its calls off the conversation, so they land late, and
        the run's times are when the turn happened (`open_chat_run`). Within a
        run's deadline of now and not ahead of it: anything else is a clock or
        a bug, and a 422 says so rather than a run that began last year.
        """
        if value is None:
            return None
        try:
            moment = datetime.fromisoformat(str(value))
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=f"{name} is an ISO time") from exc
        now = datetime.now(UTC)
        if moment.tzinfo is None or not (
            now - timedelta(seconds=RUN_DEADLINE_S) <= moment <= now + timedelta(seconds=5)
        ):
            raise HTTPException(status_code=422, detail=f"{name} is not a time of this turn")
        return moment

    @app.post("/api/boxes/{box_id}/chat-runs", status_code=201)
    def open_chat_run(
        box_id: str, body: dict[str, Any], token: Token | None = needs_fleet_route
    ) -> Any:
        """`{prompt, session_id, by, client, started_at, stored_session_id?}`: a turn
        began; `stored_session_id` is the conversation it is listed and opened
        by, when the door heard which (FLOTTA-173). 409 while the agent
        is doing another run, and the turn is then not recorded: its model
        calls belong to that run (`FleetStore._run_for_call`)."""
        session_id, by = body.get("session_id"), body.get("by")
        for name, value in (("session_id", session_id), ("by", by)):
            if not isinstance(value, str) or not 0 < len(value) <= 200:
                raise HTTPException(
                    status_code=422, detail=f"{name} is a name of 1 to 200 characters"
                )
        stored = body.get("stored_session_id")
        if stored is not None and (not isinstance(stored, str) or not 0 < len(stored) <= 200):
            raise HTTPException(
                status_code=422, detail="stored_session_id is a name of 1 to 200 characters"
            )
        prompt = body.get("prompt")
        prompt = prompt if isinstance(prompt, str) and prompt.strip() else CHAT_NO_PROMPT
        started_at = _door_time(body.get("started_at"), "started_at")
        store = store_factory()
        try:
            box = _routable_box(store, token, box_id)
            try:
                run = store.open_chat_run(
                    box.id,
                    prompt,
                    input={
                        "session_id": session_id,
                        "by": by,
                        "client": bool(body.get("client")),
                        **({"stored_session_id": stored} if stored else {}),
                    },
                    started_at=started_at,
                )
            except BoxBusyError as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            except UnknownEntityError as exc:
                raise HTTPException(status_code=404, detail=f"no box {box_id!r}") from exc
            store.add_event("task", run.id, "created", {"trigger": "chat", "by": by})
            return {"id": run.id, "status": run.status}
        finally:
            store.close()

    @app.post("/api/boxes/{box_id}/runs/{run_id}/steps")
    def finish_chat_run(
        box_id: str, run_id: str, body: dict[str, Any], token: Token | None = needs_fleet_route
    ) -> Any:
        """`{steps, status, output?, error_class?, approvals?, ended_at?}`: the turn ended,
        `done` or `failed`. Only a chat run of this box, still running."""
        steps = body.get("steps") or []
        status = body.get("status")
        if not isinstance(steps, list) or not all(isinstance(x, dict) for x in steps):
            raise HTTPException(status_code=422, detail="steps is a list of objects")
        if status not in ("done", "failed"):
            raise HTTPException(status_code=422, detail="a turn ends done or failed")
        output, error_class = body.get("output"), body.get("error_class")
        approvals = body.get("approvals")
        ended_at = _door_time(body.get("ended_at"), "ended_at")
        store = store_factory()
        try:
            box = _routable_box(store, token, box_id)
            try:
                run = store.finish_chat_run(
                    run_id,
                    box.id,
                    status,
                    output=output if isinstance(output, str) else None,
                    steps=steps[:CHAT_MAX_STEPS_SENT],
                    approval_count=approvals if isinstance(approvals, int) else None,
                    error_class=str(error_class)[:80] if error_class else None,
                    now=ended_at,
                )
            except UnknownEntityError as exc:
                raise HTTPException(status_code=404, detail=f"no chat run {run_id!r}") from exc
            except InvalidTransitionError as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            store.add_event("task", run.id, status, {"trigger": "chat"})
            return {"id": run.id, "status": run.status}
        finally:
            store.close()

    # -- a conversation's approvals (FLOTTA-166) --------------------------------
    # The door holds a person's conversation, so it records what the agent asks
    # there against the turn's chat run: then `GET /api/approvals` lists it next
    # to a delivered run's, and `POST /api/runs/{id}/approval` answers it, which
    # the door carries to the box on that socket. Only the door, for the box
    # whose socket it heard, and only a chat run of that box.

    def _door_chat_run(store: FleetStore, token: Token | None, box_id: str, run_id: str) -> Any:
        box = _routable_box(store, token, box_id)
        try:
            return store.chat_run_of(run_id, box.id)
        except UnknownEntityError as exc:
            raise HTTPException(status_code=404, detail=f"no chat run {run_id!r}") from exc

    @app.post("/api/boxes/{box_id}/runs/{run_id}/approvals", status_code=201)
    def ask_chat_approval(
        box_id: str, run_id: str, body: dict[str, Any], token: Token | None = needs_fleet_route
    ) -> Any:
        """`{request_id, command?, description?, choices?}`: the agent asked a
        person, in this turn, as the box's request `request_id`."""
        from flotta.box.approvals import FLOTTA_APPROVAL_TIMEOUT_S
        from flotta.runner import DEFAULT_CHOICES

        request_id = body.get("request_id")
        if not isinstance(request_id, str) or not 0 < len(request_id) <= 200:
            raise HTTPException(status_code=422, detail="request_id is the box's request id")
        choices = body.get("choices")
        if isinstance(choices, list) and choices and all(isinstance(c, str) for c in choices):
            offered = [c[:40] for c in choices[:10]]
        else:
            offered = list(DEFAULT_CHOICES)

        def text(value: Any) -> str | None:
            return value[:2000] if isinstance(value, str) else None

        store = store_factory()
        try:
            run = _door_chat_run(store, token, box_id, run_id)
            if run.status != "running":
                raise HTTPException(status_code=409, detail=f"run {run_id!r} is {run.status}")
            approval = store.ask_approval(
                run.id,
                request_id=request_id,
                command=text(body.get("command")),
                description=text(body.get("description")),
                choices=offered,
                expires_at=datetime.now(UTC) + timedelta(seconds=FLOTTA_APPROVAL_TIMEOUT_S),
            )
            return {"id": approval.id, "status": approval.status}
        finally:
            store.close()

    @app.get("/api/boxes/{box_id}/runs/{run_id}/approvals")
    def held_chat_approvals(
        box_id: str, run_id: str, token: Token | None = needs_fleet_route
    ) -> Any:
        """The turn's approvals, for the door that holds its socket: reading them
        says it is still listening (`hold_approvals`), so they stay answerable."""
        from dataclasses import asdict

        store = store_factory()
        try:
            run = _door_chat_run(store, token, box_id, run_id)
            store.hold_approvals(run.id)
            return {"approvals": [asdict(a) for a in store.run_approvals(run.id)]}
        finally:
            store.close()

    @app.post("/api/boxes/{box_id}/runs/{run_id}/approvals/settled")
    def settle_chat_approval(
        box_id: str, run_id: str, body: dict[str, Any], token: Token | None = needs_fleet_route
    ) -> Any:
        """`{request_id, choice?, by?}`: settled on the socket itself. With a
        `choice`, the person there answered it; without, the box withdrew it."""
        from dataclasses import asdict

        request_id, choice, by = body.get("request_id"), body.get("choice"), body.get("by")
        if not isinstance(request_id, str) or not request_id:
            raise HTTPException(status_code=422, detail="request_id is the box's request id")
        if choice is not None and not isinstance(choice, str):
            raise HTTPException(status_code=422, detail="choice is a string")
        store = store_factory()
        try:
            run = _door_chat_run(store, token, box_id, run_id)
            settled = store.settle_approval(
                run.id,
                request_id,
                choice=choice[:40] if choice else None,
                by=by[:200] if isinstance(by, str) else None,
            )
            if settled is None:
                raise HTTPException(status_code=404, detail=f"no approval {request_id!r}")
            return asdict(settled)
        finally:
            store.close()

    @app.post("/api/boxes/{box_id}/runs/{run_id}/approvals/{approval_id}/delivered")
    def chat_approval_delivered(
        box_id: str, run_id: str, approval_id: str, token: Token | None = needs_fleet_route
    ) -> Any:
        """The door carried a person's answer from outside to the box."""
        store = store_factory()
        try:
            run = _door_chat_run(store, token, box_id, run_id)
            approval = store.get_approval(approval_id)
            if approval is None or approval.run_id != run.id:
                raise HTTPException(status_code=404, detail=f"no approval {approval_id!r}")
            store.mark_delivered(approval.id)
            return {"id": approval.id}
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
        from datetime import UTC

        from flotta.budgets import paused_message
        from flotta.provision import BoxPaused, ProvisionError, suspended_wake_message, wake_box

        store = store_factory()
        try:
            box = _routable_box(store, token, box_id)
            # Before the activity: a tab left open must not hold a paused machine
            # awake, or a suspended org's (FLOTTA-184). 402 for both, which the door
            # closes with as 4402 and the control plane's words; the web app tells
            # them apart by the org's status on `/api/me`.
            owner = store.get_org(box.org)
            if owner is not None and owner.status == "suspended":
                raise HTTPException(status_code=402, detail=suspended_wake_message(box, owner.id))
            if box.status == "paused":
                raise HTTPException(
                    status_code=402, detail=paused_message(store, box, datetime.now(UTC))
                )
            _record_activity(store, box.id)
            try:
                result = wake_box(box.id, store=store, reason="front-door")
            except BoxPaused as exc:
                raise HTTPException(status_code=402, detail=str(exc)) from exc
            except ProvisionError as exc:
                # 409 for an illegal state (a torn-down box cannot be woken),
                # 502 for the substrate failing to start it. The caller can act
                # on the first and only retry the second.
                status = 409 if "only a running or stopped box" in str(exc) else 502
                raise HTTPException(status_code=status, detail=str(exc)) from exc
            return {"box": _box_dict(_routable_box(store, token, box.id)), **result}
        finally:
            store.close()

    @app.post("/api/boxes/{box_id}/unpause")
    def unpause_box_endpoint(box_id: str, token: Token | None = needs_admin) -> Any:
        """Let a box a budget paused be woken again, now rather than a sweep later.

        409 while a budget still holds it, naming the budget: the sweep would only
        pause it again, so the answer is to raise the limit. See `flotta.budgets`.
        """
        from flotta.provision import BudgetHolds, ProvisionError, unpause_box

        store = store_factory()
        try:
            box = _operated_box(store, token, box_id)
            try:
                result = unpause_box(box.id, store=store)
            except BudgetHolds as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            except ProvisionError as exc:
                raise HTTPException(status_code=502, detail=str(exc)) from exc
            return {"box": _box_dict(_operated_box(store, token, box.id)), **result}
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
            answers_approvals = store.clients_answer_approvals(box.id)
        finally:
            store.close()
        ticket = mint_ticket(
            subject=token.subject if token is not None else "operator",
            box_id=box.id,
            org=box.org,
            client=box.client,
            key=key,
            # Someone who can change the agent anyway. Anyone else gets a
            # conversation and no more, which is what a ticket says by default.
            full_access=token is None or token.allows(SCOPE_FLEET_WRITE),
            answers_approvals=answers_approvals,
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

    from flotta.server_logs import log_config

    check_bind(host)
    described = (
        db.describe_url(os.environ.get(db.DATABASE_URL_ENV, ""))
        if os.environ.get(db.DATABASE_URL_ENV)
        else "a local SQLite file"
    )
    _log.info("control plane on %s:%s, fleet state in %s", host, port, described)
    # proxy_headers off: `caller_address` is the one reader of X-Forwarded-For.
    from flotta.provision import warm_release_cache

    uvicorn.run(
        create_app(warm_release_cache=warm_release_cache),
        host=host,
        port=port,
        proxy_headers=False,
        log_config=log_config(),
    )
