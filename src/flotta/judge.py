"""Jev, TypeSafe AI's decision model, judging a client's request (FLOTTA-173).

An agent files a request when someone in a conversation asks it for a piece of
work (`flotta-request`). Before the agent starts, Jev says three things about
it: what **kind** of work it is, how **complex**, and how **urgent**. The
agency decides, per agent, the most complex work the agent may start on its own
(`box_request_policy.auto_level`); anything above it waits for a person.

Jev is a "System One" model: it answers typed questions with calibrated
probabilities rather than prose (https://docs.typesafe.ai/introduction). One
call, `POST https://api.typesafe.ai/v1/systemone`, with the request as the
`state` and three questions: a `choice` for the kind and a `score` each for
complexity and priority. Each score comes back with a probability per level,
which is what the decision uses: a request starts on its own only when Jev
puts at least `min_confidence` of its probability at or below the agent's
level. "Probably small, possibly large" waits for a person.

**Every failure waits for a person.** No key, a timeout, a refusal, an answer
that does not parse: the request is held, with the reason, and never started
because the judge could not be asked. A judge that fails open is no judge.

The key is `$TYPESAFE_API_KEY`, on the control plane only. Its cost is not
metered against the agency: $0.042 per million input tokens is a few hundred
millionths of a euro a request.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

JEV_URL = "https://api.typesafe.ai/v1/systemone"
JEV_MODEL = "jev-latest"
KEY_ENV = "TYPESAFE_API_KEY"
#: Jev answers in milliseconds; this sits in front of an agent waiting to start.
TIMEOUT_S = 10.0
#: Characters of a request Jev is shown: its context is 32k tokens.
MAX_STATE = 12_000

#: What kind of work it is: the `choice` question's options, with what each means.
KINDS: dict[str, str] = {
    "feature": "Something new: a feature, a page, an integration, a new report",
    "fix": "Something broken that should work: a bug, an error, a failure",
    "change": "Changing something that works: wording, styling, configuration",
    "research": "Finding something out: an investigation, an analysis, advice",
    "operations": "Running something: a deployment, a migration, an import, access",
    "other": "None of these",
}

#: How much work it is, low to high: the `score` question's levels.
COMPLEXITY: tuple[str, ...] = (
    "Trivial: minutes, one obvious step, nothing to decide",
    "Small: under an hour, clear what to do, one place to change",
    "Medium: a few hours, several parts, some choices to make along the way",
    "Large: a day or more, touches many parts, needs a plan before starting",
    "A project: needs design and planning with people before any work starts",
)

#: The words the agency sees for each complexity level.
COMPLEXITY_NAMES: tuple[str, ...] = ("trivial", "small", "medium", "large", "project")
#: The same, as a sentence says them: "judged a project".
_SAID: tuple[str, ...] = ("trivial", "small", "medium", "large", "a project")

#: How urgent it is, low to high.
PRIORITY: tuple[str, ...] = (
    "Low: whenever there is time, nothing waits on it",
    "Normal: should be done soon, in the usual course of work",
    "High: someone is blocked or losing something until it is done",
    "Urgent: something is down or harmful now, every hour matters",
)

PRIORITY_NAMES: tuple[str, ...] = ("low", "normal", "high", "urgent")

#: The agent's level when the agency has not chosen one: trivial and small work
#: starts on its own, medium and above waits.
DEFAULT_AUTO_LEVEL = 1
#: Never starts on its own: everything waits for a person.
NEVER = -1
#: How sure Jev must be that the work is within the agent's level.
DEFAULT_MIN_CONFIDENCE = 0.7
#: How far from 1 a score's probabilities may add up before the answer is not
#: trusted: rounding, not a missing share.
SUM_TOLERANCE = 0.02

#: `(url, *, headers, json, timeout) -> (status, body)`.
Post = Callable[..., tuple[int, Any]]


class JudgeError(Exception):
    """Jev could not be asked, or its answer could not be read: words for a person."""


@dataclass(frozen=True, slots=True)
class Verdict:
    """What Jev said about one request."""

    kind: str
    #: The most likely complexity level, an index into `COMPLEXITY`.
    complexity: int
    #: The probability of each complexity level, low to high.
    complexity_p: tuple[float, ...]
    priority: int
    priority_p: tuple[float, ...]
    #: Jev's own confidence in each answer, 0 to 1.
    confidence: dict[str, float] = field(default_factory=dict)
    model: str = JEV_MODEL

    def within(self, level: int) -> float:
        """The probability that the work is at or below `level`."""
        if level < 0:
            return 0.0
        return round(sum(self.complexity_p[: level + 1]), 4)

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "complexity": self.complexity,
            "complexity_p": list(self.complexity_p),
            "priority": self.priority,
            "priority_p": list(self.priority_p),
            "confidence": dict(self.confidence),
            "model": self.model,
        }


def questions() -> dict[str, Any]:
    """The three questions, as Jev's API takes them."""
    return {
        "kind": {
            "type": "choice",
            "instructions": "What kind of work the person is asking for",
            "criteria": dict(KINDS),
        },
        "complexity": {
            "type": "score",
            "instructions": (
                "How much work it is for a capable engineer or analyst who knows the "
                "project, from what is asked"
            ),
            "criteria": list(COMPLEXITY),
        },
        "priority": {
            "type": "score",
            "instructions": "How urgent it is for the person asking",
            "criteria": list(PRIORITY),
        },
    }


def state(title: str, details: str) -> str:
    """What Jev is shown: the request as the agent filed it."""
    text = f"Request: {title.strip()}\n\nDetails:\n{details.strip() or '(none given)'}"
    return text[:MAX_STATE]


def _httpx_post(url: str, **kwargs: Any) -> tuple[int, Any]:
    import httpx

    response = httpx.post(url, **kwargs)
    try:
        return response.status_code, response.json()
    except ValueError:
        return response.status_code, {}


def _levels(answer: Any, count: int) -> tuple[int, tuple[float, ...]]:
    """A score's answer as (most likely level, probability per level)."""
    if not isinstance(answer, dict) or not isinstance(answer.get("probabilities"), dict):
        raise JudgeError("Jev's answer had no probabilities")
    probabilities = []
    for level in range(count):
        raw = answer["probabilities"].get(str(level))
        if raw is None:
            # Scaling the others up to fill the gap could carry the request under
            # the agent's level when the missing share sat above it.
            raise JudgeError(f"Jev's answer had no probability for level {level}")
        if isinstance(raw, bool) or not isinstance(raw, int | float) or not 0 <= raw <= 1:
            raise JudgeError(f"Jev's probability for level {level} is not one")
        probabilities.append(float(raw))
    total = sum(probabilities)
    if abs(total - 1) > SUM_TOLERANCE:
        raise JudgeError(f"Jev's probabilities add up to {total:.3f}, not 1")
    probabilities = [p / total for p in probabilities]  # only rounding is taken out
    best = max(range(count), key=lambda i: probabilities[i])
    return best, tuple(round(p, 4) for p in probabilities)


def judge(
    title: str,
    details: str,
    *,
    env: Mapping[str, str] | None = None,
    post: Post | None = None,
) -> Verdict:
    """Ask Jev about one request, or `JudgeError` saying why it could not be asked."""
    environ = os.environ if env is None else env
    key = (environ.get(KEY_ENV) or "").strip()
    if not key:
        raise JudgeError(f"no ${KEY_ENV} is set, so nothing judges requests")
    caller = post or _httpx_post
    try:
        status, body = caller(
            JEV_URL,
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            json={"model": JEV_MODEL, "state": state(title, details), "questions": questions()},
            timeout=TIMEOUT_S,
        )
    except Exception as exc:  # noqa: BLE001 - any transport failure is "could not ask"
        raise JudgeError(f"could not reach Jev: {type(exc).__name__}") from exc
    if status != 200 or not isinstance(body, dict):
        detail = body.get("message") or body.get("error") if isinstance(body, dict) else None
        raise JudgeError(f"Jev answered {status}{f': {detail}' if detail else ''}")
    answers = body.get("answers")
    if not isinstance(answers, dict):
        raise JudgeError("Jev's answer had no answers")
    kind_answer = answers.get("kind")
    kind = kind_answer.get("choice") if isinstance(kind_answer, dict) else None
    if kind not in KINDS:
        kind = "other"
    complexity, complexity_p = _levels(answers.get("complexity"), len(COMPLEXITY))
    priority, priority_p = _levels(answers.get("priority"), len(PRIORITY))
    confidence = {
        name: float(a["confidence"])
        for name, a in answers.items()
        if isinstance(a, dict) and isinstance(a.get("confidence"), int | float)
    }
    return Verdict(
        kind=kind,
        complexity=complexity,
        complexity_p=complexity_p,
        priority=priority,
        priority_p=priority_p,
        confidence=confidence,
        model=str(body.get("model") or JEV_MODEL),
    )


@dataclass(frozen=True, slots=True)
class Decision:
    """Whether a request may start without a person, and why, in words."""

    starts: bool
    why: str


def decide(verdict: Verdict | None, *, auto_level: int, min_confidence: float) -> Decision:
    """Start it, or hold it for the agency. Without a verdict, it is held."""
    if verdict is None:
        return Decision(False, "it could not be judged, so a person decides")
    if auto_level < 0:
        return Decision(False, "every request this agent is given waits for the agency")
    name = _SAID[verdict.complexity]
    limit = _SAID[min(auto_level, len(_SAID) - 1)]
    within = verdict.within(auto_level)
    if within >= min_confidence:
        return Decision(
            True, f"judged {name}, within what this agent may start on its own ({limit})"
        )
    if verdict.complexity <= auto_level:
        return Decision(
            False,
            f"judged {name}, but with only {round(within * 100)}% likelihood of being {limit} "
            "or less, so a person decides",
        )
    return Decision(False, f"judged {name}, more than this agent may start on its own ({limit})")
