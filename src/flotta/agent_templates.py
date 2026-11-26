"""The operator's changes to the templates agents are made from (FLOTTA-269).

**The templates are the front end's.** A web app or a dashboard ships its own
list: what each is called, what its agent is told, what it is set up with. The
control plane does not know that list. What it keeps is, per template id, the
parts the fleet's operator **changed**, so a change needs no release of the
front end, and every front end reading this fleet shows the same one.

**Changes, not a copy.** Only the parts named here are kept. A part left out is
whatever the front end ships, so a later release that improves a template's
rules still reaches a fleet whose operator only rewrote its instructions.
`None` for a part that can be absent (`pull_requests`, `starts_alone`,
`schedule`, `rules`) says the operator took it away, which is not the same as
leaving it alone.

**What a saved change promises:** it can make an agent. Each part is held to
the limit the route that later applies it holds: instructions to what
`POST /api/boxes` takes, rules to what an agent's rules take, a schedule to
what a trigger takes, a tool to one the library has.

**What is never changed here:** a template's id, which agents record
(`boxes.template`), and what it is called. A front end's name for a template is
its own, often in several languages, and one stored name would disagree with
all but one of them.
"""

from __future__ import annotations

from typing import Any

from flotta.mcp_library import entry as library_entry
from flotta.rules import MAX_TEXT, RulesError, parse_rules
from flotta.schedules import check as check_schedule
from flotta.store import (
    DESCRIPTION_MAX,
    DISPLAY_NAME_MAX,
    INSTRUCTIONS_MAX,
    MAX_TRIGGER_PROMPT,
)

#: The parts of a template the operator may change.
FIELDS = (
    "offered",
    "name",
    "description",
    "instructions",
    "screen",
    "repositories",
    "pull_requests",
    "starts_alone",
    "tools",
    "schedule",
    "rules",
)

#: Tools one template may name. The library has eleven.
MAX_TOOLS = 20

#: The work an agent may start alone, as a request level: -1 is "always ask".
STARTS_ALONE = range(-1, 5)

#: A schedule here has no time zone: it is read in the zone of whoever makes
#: the agent. It is checked in this one, which every zone agrees with on how
#: often five cron fields fire.
_ANY_ZONE = "UTC"


class AgentTemplateError(ValueError):
    """A change to a template that cannot be kept, with why."""


def parse_changes(body: Any) -> dict[str, Any]:
    """An operator's changes to one template, tidied, or `AgentTemplateError`.

    Only the parts given come back, in `FIELDS` order, as plain JSON values.
    """
    if not isinstance(body, dict):
        raise AgentTemplateError("changes are an object")
    unknown = sorted(set(body) - set(FIELDS))
    if unknown:
        raise AgentTemplateError(f"not a part of a template that can be changed: {unknown[0]}")
    out: dict[str, Any] = {}
    for field in FIELDS:
        if field in body:
            out[field] = _PARSERS[field](body[field])
    return out


def public_view(changes: dict[str, Any]) -> dict[str, Any]:
    """What someone who is not signed in may know of these changes.

    Whether the template is offered, and what its agent is set up with, as a
    page that lists the templates says it before sign-up. **Never the
    operator's words**: not the instructions, the description, a rule's text or
    what a schedule tells the agent.
    """
    out: dict[str, Any] = {}
    for field in ("offered", "screen", "repositories", "pull_requests", "tools"):
        if field in changes:
            out[field] = changes[field]
    if "schedule" in changes:
        out["schedule"] = changes["schedule"] is not None
    if "rules" in changes:
        rules = changes["rules"]
        out["rules"] = None if rules is None else {"refused": rules["refused"] is not None}
    return out


def _flag(what: str):
    def parse(value: Any) -> bool:
        if not isinstance(value, bool):
            raise AgentTemplateError(f"{what} is true or false")
        return value

    return parse


def _text(what: str, limit: int):
    def parse(value: Any) -> str:
        if not isinstance(value, str):
            raise AgentTemplateError(f"{what} is text")
        cleaned = value.strip()
        if "\x00" in cleaned:
            raise AgentTemplateError(f"{what} contains a NUL byte")
        if len(cleaned) > limit:
            raise AgentTemplateError(f"{what} is too long ({len(cleaned)} > {limit} characters)")
        return cleaned

    return parse


def _pull_requests(value: Any) -> dict[str, bool] | None:
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) != {"may_merge", "follow_reviews"}:
        raise AgentTemplateError(
            'pull_requests is {"may_merge": true or false, "follow_reviews": true or false}, '
            "or null for the fleet's default"
        )
    if not all(isinstance(v, bool) for v in value.values()):
        raise AgentTemplateError("may_merge and follow_reviews are true or false")
    return {"may_merge": value["may_merge"], "follow_reviews": value["follow_reviews"]}


def _starts_alone(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value not in STARTS_ALONE:
        raise AgentTemplateError(
            f"starts_alone is a whole number from {STARTS_ALONE.start} to "
            f"{STARTS_ALONE.stop - 1}, or null for the fleet's default"
        )
    return value


def _tools(value: Any) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise AgentTemplateError("tools is a list of the library's ids")
    kept: list[str] = []
    for item in value:
        if library_entry(item) is None:
            raise AgentTemplateError(f"no tool {item!r} in the library")
        if item not in kept:
            kept.append(item)
    if len(kept) > MAX_TOOLS:
        raise AgentTemplateError(f"a template names at most {MAX_TOOLS} tools")
    return kept


def _schedule(value: Any) -> dict[str, str] | None:
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) != {"cron", "prompt"}:
        raise AgentTemplateError('schedule is {"cron": "...", "prompt": "..."}, or null for none')
    cron, prompt = value["cron"], value["prompt"]
    if not isinstance(cron, str) or not isinstance(prompt, str):
        raise AgentTemplateError("a schedule's cron and prompt are text")
    try:
        expr = check_schedule(cron, _ANY_ZONE).expr
    except ValueError as exc:
        raise AgentTemplateError(str(exc)) from exc
    told = prompt.strip()
    if not told:
        raise AgentTemplateError("a schedule needs a prompt: what the agent is to do each time")
    if len(told) > MAX_TRIGGER_PROMPT:
        raise AgentTemplateError(f"a schedule's prompt is at most {MAX_TRIGGER_PROMPT} characters")
    return {"cron": expr, "prompt": told}


def _rules(value: Any) -> dict[str, Any] | None:
    """`{"never": [...], "careful": [...], "refused": {"says", "patterns"} | null}`.

    The lists are held to an agent's own rules (`flotta.rules`), since that is
    what they become. `refused` carries `says` beside its patterns because a
    pattern reads like code and a person has to be told what it stops. Rules
    with nothing in them are no rules.
    """
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) - {"never", "careful", "refused"}:
        raise AgentTemplateError(
            'rules is {"never": [...], "careful": [...], "refused": {...}}, or null for none'
        )
    refused = value.get("refused")
    if refused is not None and (
        not isinstance(refused, dict) or set(refused) != {"says", "patterns"}
    ):
        raise AgentTemplateError('refused is {"says": "...", "patterns": [...]}, or null')
    try:
        parsed = parse_rules(
            {
                "never": value.get("never"),
                "careful": value.get("careful"),
                "denied_commands": None if refused is None else refused["patterns"],
            }
        )
    except RulesError as exc:
        raise AgentTemplateError(str(exc).replace("denied_commands", "refused patterns")) from exc
    kept: dict[str, Any] | None = None
    if parsed.denied_commands:
        says = _text("what refused commands are called", MAX_TEXT)(refused["says"])
        if not says:
            raise AgentTemplateError(
                "refused commands need words for people: what they stop, in a few words"
            )
        kept = {"says": says, "patterns": list(parsed.denied_commands)}
    if not (parsed.never or parsed.careful or kept):
        return None
    return {"never": list(parsed.never), "careful": list(parsed.careful), "refused": kept}


_PARSERS = {
    "offered": _flag("offered"),
    "name": _text("the starting name", DISPLAY_NAME_MAX),
    "description": _text("the description", DESCRIPTION_MAX),
    "instructions": _text("the instructions", INSTRUCTIONS_MAX),
    "screen": _flag("screen"),
    "repositories": _flag("repositories"),
    "pull_requests": _pull_requests,
    "starts_alone": _starts_alone,
    "tools": _tools,
    "schedule": _schedule,
    "rules": _rules,
}
