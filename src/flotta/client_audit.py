"""What happened for one client, in one window (M16.3): its audit trail and the
numbers its monthly report is made from.

Both read the same rows (`FleetStore.client_window`): the client's runs, the
approvals answered in them, its requests, the pull requests its agents opened
or saw merged, and what happened to its agents themselves. The audit trail
lists them as entries; the report data adds them up. Neither writes anything.

**A client's viewer reads its own trail, without the agency's words** (D27):
no run's prompt or input, no request's details beyond its title, and an
agent's own events by what happened, never with what was set (an agent's
instructions are among them). Conversations are left out for a viewer, as
they are from its runs.
"""

from __future__ import annotations

import re
from collections import Counter
from datetime import UTC, datetime, timedelta
from typing import Any

from flotta.store import FleetStore

#: The longest window one call reads: a quarter, so an export of a year is
#: four calls and none of them reads the whole ledger.
MAX_WINDOW = timedelta(days=92)

#: The most entries one call answers; `truncated` says there were more.
MAX_ENTRIES = 5000

#: Run statuses that are a failure a report names as an incident.
FAILED = frozenset({"failed", "budget_blocked", "timed_out"})


class BadWindow(ValueError):
    """A window that is not one, or is longer than `MAX_WINDOW`."""


def month_window(period: str) -> tuple[datetime, datetime]:
    """`YYYY-MM` as `[its first moment, the next month's)`, in UTC."""
    if not re.fullmatch(r"\d{4}-\d{2}", period or ""):
        raise BadWindow(f"a period is a month as YYYY-MM, got {period!r}")
    try:
        start = datetime.strptime(period, "%Y-%m").replace(tzinfo=UTC)
    except ValueError as exc:
        raise BadWindow(f"a period is a month as YYYY-MM, got {period!r}") from exc
    end = (
        start.replace(year=start.year + 1, month=1)
        if start.month == 12
        else start.replace(month=start.month + 1)
    )
    return start, end


def check_window(since: datetime, until: datetime) -> None:
    if until <= since:
        raise BadWindow("`to` comes after `from`")
    if until - since > MAX_WINDOW:
        raise BadWindow(f"a window is at most {MAX_WINDOW.days} days: ask for it in parts")


def audit(
    store: FleetStore,
    *,
    org: str,
    client: str,
    since: datetime,
    until: datetime,
    viewer: bool,
) -> dict[str, Any]:
    """The client's trail in `[since, until)`, oldest first."""
    check_window(since, until)
    window = store.client_window(
        org, client, since, until, exclude_kinds=("chat",) if viewer else ()
    )
    names = {b.id: b.name for b in window.boxes}
    entries: list[dict[str, Any]] = []

    def add(at: str | None, box_id: str, kind: str, actor: str | None, summary: str, **extra: Any):
        if at is None or not (window.since <= at < window.until):
            return
        entries.append(
            {
                "at": at,
                "agent": names.get(box_id, box_id),
                "kind": kind,
                "actor": actor,
                "summary": summary,
                **extra,
            }
        )

    for run in window.runs:
        add(
            run.created_at,
            run.box_id,
            "run.started",
            run.trigger_kind,
            f"A {run.trigger_kind} run started",
            run=run.id,
        )
        if run.finished_at:
            add(
                run.finished_at,
                run.box_id,
                "run.finished",
                run.trigger_kind,
                f"A {run.trigger_kind} run ended {run.status}",
                run=run.id,
                status=run.status,
                **({"error_class": run.error_class} if run.error_class else {}),
            )
    for approval in window.approvals:
        add(
            approval.answered_at,
            approval.box_id,
            "approval.answered",
            approval.answered_by,
            f"Answered {approval.choice!r}: {approval.description or 'a request for permission'}",
            run=approval.run_id,
            choice=approval.choice,
        )
    for request in window.requests:
        add(
            request.created_at,
            request.box_id,
            "request.made",
            request.asked_by,
            f"Asked: {request.title}",
            request=request.id,
        )
        if request.decided_at:
            add(
                request.decided_at,
                request.box_id,
                f"request.{request.status}",
                "auto" if request.auto else request.decided_by,
                f"{request.status.capitalize()}: {request.title}",
                request=request.id,
            )
    for pull in window.pulls:
        add(
            pull.opened_at,
            pull.box_id,
            "pull.opened",
            pull.author_login,
            f"Opened {pull.repo}#{pull.number}: {pull.title}",
            url=pull.url,
        )
        if pull.merged_at:
            add(
                pull.merged_at,
                pull.box_id,
                "pull.merged",
                pull.merged_by_person or pull.merged_by,
                f"Merged {pull.repo}#{pull.number}: {pull.title}",
                url=pull.url,
            )
    for event in window.events:
        payload = None if viewer else event.payload
        add(
            event.ts,
            event.entity_id,
            f"agent.{event.type}",
            (event.payload or {}).get("by") if not viewer else None,
            event.type.replace("_", " "),
            **({"payload": payload} if payload else {}),
        )

    entries.sort(key=lambda e: (e["at"], e["kind"]))
    truncated = len(entries) > MAX_ENTRIES
    return {
        "org": org,
        "client": client,
        "from": window.since,
        "to": window.until,
        "entries": entries[:MAX_ENTRIES],
        "truncated": truncated,
    }


def report_data(store: FleetStore, *, org: str, client: str, period: str) -> dict[str, Any]:
    """What a client's monthly report adds up, for `period` (`YYYY-MM`)."""
    since, until = month_window(period)
    window = store.client_window(org, client, since, until)
    names = {b.id: b.name for b in window.boxes}
    metas = store.meta_for_boxes([b.id for b in window.boxes])
    labels = {
        b.id: (metas[b.id].display_name if b.id in metas and metas[b.id].display_name else b.name)
        for b in window.boxes
    }

    in_window = [r for r in window.runs if window.since <= r.created_at < window.until]
    work = [r for r in in_window if r.trigger_kind != "chat"]
    per_agent: dict[str, Counter[str]] = {}
    for run in work:
        per_agent.setdefault(run.box_id, Counter())[run.status] += 1
    conversations = Counter(r.box_id for r in in_window if r.trigger_kind == "chat")
    failures = [
        {
            "run": r.id,
            "agent": names.get(r.box_id, r.box_id),
            "at": r.finished_at or r.created_at,
            "status": r.status,
            "error_class": r.error_class,
            "resolved": r.resolved_at is not None,
        }
        for r in work
        if r.status in FAILED
    ]
    spend = store.spend(org=org, since=since, client=client)
    until_spend = store.spend(org=org, since=until, client=client)
    return {
        "org": org,
        "client": client,
        "period": period,
        "from": window.since,
        "to": window.until,
        "agents": [
            {
                "id": b.id,
                "name": b.name,
                "label": labels[b.id],
                "runs": dict(per_agent.get(b.id, Counter())),
                "conversations": conversations.get(b.id, 0),
            }
            for b in window.boxes
        ],
        "runs": {"total": len(work), **dict(sum(per_agent.values(), Counter()))},
        "conversations": sum(conversations.values()),
        "approvals_answered": sum(
            1
            for a in window.approvals
            if a.answered_at and window.since <= a.answered_at < window.until
        ),
        "requests": {
            "made": sum(1 for q in window.requests if window.since <= q.created_at < window.until),
            "approved": sum(
                1
                for q in window.requests
                if q.status == "approved"
                and q.decided_at
                and window.since <= q.decided_at < window.until
            ),
            "declined": sum(
                1
                for q in window.requests
                if q.status == "declined"
                and q.decided_at
                and window.since <= q.decided_at < window.until
            ),
        },
        "pull_requests": {
            "opened": [
                {
                    "repo": p.repo,
                    "number": p.number,
                    "title": p.title,
                    "url": p.url,
                    "state": p.state,
                }
                for p in window.pulls
                if window.since <= p.opened_at < window.until
            ],
            "merged": sum(
                1
                for p in window.pulls
                if p.merged_at and window.since <= p.merged_at < window.until
            ),
        },
        "failures": failures,
        "spend_eur_nano": spend.eur_nano - until_spend.eur_nano,
        "limit_eur_nano": _monthly_limit(store, org, client),
    }


def _monthly_limit(store: FleetStore, org: str, client: str) -> int | None:
    """The client's own monthly budget, if it has one."""
    for budget in store.budgets_for_org(org):
        if budget.client == client and budget.box is None and budget.period == "month":
            return budget.limit_eur_nano
    return None
