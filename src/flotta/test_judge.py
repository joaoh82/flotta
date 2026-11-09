"""Jev judging a request (FLOTTA-173). Jev is a fake that answers as its docs say."""

from __future__ import annotations

import pytest

from flotta import judge
from flotta.judge import JEV_URL, JudgeError, Verdict, decide

ENV = {"TYPESAFE_API_KEY": "ts-key"}


def _score(probabilities: list[float], confidence: float = 0.9) -> dict:
    return {
        "type": "score",
        "score": sum(i * p for i, p in enumerate(probabilities)),
        "confidence": confidence,
        "probabilities": {str(i): p for i, p in enumerate(probabilities)},
    }


def _answer(complexity=(0.1, 0.8, 0.1, 0, 0), priority=(0, 1, 0, 0), kind="feature") -> dict:
    return {
        "model": "jev-1.13.0",
        "answers": {
            "kind": {
                "type": "choice",
                "choice": kind,
                "confidence": 1.0,
                "probabilities": {kind: 1.0},
            },
            "complexity": _score(list(complexity)),
            "priority": _score(list(priority)),
        },
        "usage": {"input_tokens": 400, "output_tokens": 70},
    }


def _missing_levels() -> dict:
    """Small, with a tenth left out above it: scaled up, it would look 100% small."""
    body = _answer()
    body["answers"]["complexity"]["probabilities"] = {"0": 0.5, "1": 0.4}
    return body


class Jev:
    def __init__(self, status=200, body=None):
        self.status, self.body, self.asked = status, body or _answer(), []

    def __call__(self, url, **kwargs):
        self.asked.append((url, kwargs))
        return self.status, self.body


def test_jev_is_asked_three_typed_questions_about_the_request():
    jev = Jev()
    verdict = judge.judge("Add CSV export", "On the orders page, please", env=ENV, post=jev)
    [(url, kwargs)] = jev.asked
    assert url == JEV_URL
    assert kwargs["headers"]["Authorization"] == "Bearer ts-key"
    sent = kwargs["json"]
    assert sent["model"] == "jev-latest"
    assert "Add CSV export" in sent["state"] and "orders page" in sent["state"]
    assert sent["questions"]["kind"]["type"] == "choice"
    assert set(sent["questions"]["kind"]["criteria"]) == set(judge.KINDS)
    assert len(sent["questions"]["complexity"]["criteria"]) == 5
    assert len(sent["questions"]["priority"]["criteria"]) == 4
    assert (verdict.kind, verdict.complexity, verdict.priority) == ("feature", 1, 1)
    assert verdict.model == "jev-1.13.0"
    assert verdict.within(1) == pytest.approx(0.9)


def test_without_a_key_nothing_is_asked():
    jev = Jev()
    with pytest.raises(JudgeError, match="TYPESAFE_API_KEY"):
        judge.judge("x", "", env={}, post=jev)
    assert jev.asked == []


@pytest.mark.parametrize(
    ("jev", "match"),
    [
        (Jev(status=429, body={"message": "rate limited"}), "429: rate limited"),
        (Jev(body={"answers": None}), "no answers"),
        (Jev(body={"answers": {"kind": {}, "complexity": {}, "priority": {}}}), "no probabilities"),
        (Jev(body=_answer(complexity=(0, 0, 0, 0, 0))), "add up to 0.000, not 1"),
        (Jev(body=_answer(complexity=(0.3, 0.3, 0, 0, 0))), "add up to 0.600, not 1"),
        (Jev(body=_missing_levels()), "no probability for level 2"),
    ],
)
def test_an_answer_that_cannot_be_used_is_a_failure(jev, match):
    with pytest.raises(JudgeError, match=match):
        judge.judge("x", "", env=ENV, post=jev)


def test_probabilities_that_are_only_rounded_are_used():
    verdict = judge.judge(
        "x", "", env=ENV, post=Jev(body=_answer(complexity=(0.1, 0.8, 0.09, 0, 0)))
    )
    assert verdict.within(1) == pytest.approx(0.9 / 0.99, abs=1e-3)


def test_jev_being_down_is_a_failure():
    def down(url, **kwargs):
        raise OSError("no route")

    with pytest.raises(JudgeError, match="could not reach Jev"):
        judge.judge("x", "", env=ENV, post=down)


def test_an_unknown_kind_is_other():
    verdict = judge.judge("x", "", env=ENV, post=Jev(body=_answer(kind="poetry")))
    assert verdict.kind == "other"


def _verdict(complexity_p):
    best = max(range(5), key=lambda i: complexity_p[i])
    return Verdict(
        kind="feature",
        complexity=best,
        complexity_p=tuple(complexity_p),
        priority=1,
        priority_p=(0, 1, 0, 0),
    )


def test_within_the_agents_level_it_starts():
    decision = decide(_verdict([0.2, 0.7, 0.1, 0, 0]), auto_level=1, min_confidence=0.7)
    assert decision.starts
    assert "small" in decision.why


def test_above_the_agents_level_it_waits():
    decision = decide(_verdict([0, 0.1, 0.2, 0.6, 0.1]), auto_level=1, min_confidence=0.7)
    assert not decision.starts
    assert "judged large" in decision.why and "more than this agent may start" in decision.why
    project = decide(_verdict([0, 0, 0, 0, 1]), auto_level=1, min_confidence=0.7)
    assert "judged a project" in project.why


def test_probably_small_possibly_large_waits():
    """The probabilities, not only the likeliest level, decide."""
    decision = decide(_verdict([0.1, 0.45, 0.05, 0.4, 0]), auto_level=1, min_confidence=0.7)
    assert not decision.starts
    assert "55% likelihood" in decision.why


def test_unjudged_waits():
    assert not decide(None, auto_level=4, min_confidence=0.5).starts


def test_an_agent_that_always_asks_waits_for_everything():
    decision = decide(_verdict([1, 0, 0, 0, 0]), auto_level=judge.NEVER, min_confidence=0.5)
    assert not decision.starts
    assert "every request" in decision.why
