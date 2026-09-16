"""Checking a model id against OpenRouter's catalogue (FLOTTA-39). Hermetic."""

import urllib.error

import pytest

from flotta import model_catalog
from flotta.model_catalog import known_model

OR = "https://openrouter.ai/api/v1"


@pytest.fixture(autouse=True)
def fresh_cache(monkeypatch):
    monkeypatch.setattr(model_catalog, "_cache", None)


def _catalogue(monkeypatch, ids=None, error=None):
    calls = []

    def fetch(timeout):
        calls.append(timeout)
        if error:
            raise error
        return frozenset(ids or ())

    monkeypatch.setattr(model_catalog, "_fetch", fetch)
    return calls


def test_a_listed_model_is_known(monkeypatch):
    _catalogue(monkeypatch, {"z-ai/glm-5.2"})
    assert known_model("z-ai/glm-5.2", OR).verdict == "yes"


def test_an_unlisted_model_is_refused_with_a_suggestion(monkeypatch):
    _catalogue(monkeypatch, {"anthropic/claude-sonnet-4.5", "z-ai/glm-5.2"})
    known = known_model("anthropic/claude-sonet-4.5", OR)
    assert known.refuses
    assert "no model 'anthropic/claude-sonet-4.5'" in known.detail


def test_a_suggestion_is_offered_when_the_name_is_close(monkeypatch):
    _catalogue(monkeypatch, {"z-ai/glm-5.2"})
    assert "Did you mean z-ai/glm-5.2" in known_model("glm-5.2", OR).detail


def test_an_unreachable_catalogue_does_not_refuse(monkeypatch):
    """A catalogue that is down must not stop somebody choosing a model."""
    _catalogue(monkeypatch, error=urllib.error.URLError("no route"))
    known = known_model("z-ai/glm-5.2", OR)
    assert known.verdict == "unknown" and not known.refuses


def test_an_empty_catalogue_is_a_broken_answer_not_proof(monkeypatch):
    _catalogue(monkeypatch, set())
    assert known_model("z-ai/glm-5.2", OR).verdict == "unknown"


def test_other_endpoints_are_not_asked(monkeypatch):
    calls = _catalogue(monkeypatch, {"x"})
    assert known_model("anything", "https://proxy.internal/v1").verdict == "unknown"
    assert calls == []


def test_the_catalogue_is_read_once_and_reused(monkeypatch):
    calls = _catalogue(monkeypatch, {"z-ai/glm-5.2"})
    known_model("z-ai/glm-5.2", OR)
    known_model("nope/nope", OR)
    assert len(calls) == 1
