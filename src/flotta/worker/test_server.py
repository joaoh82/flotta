"""Tests for the server cores: auth, health payload, run_task timeout/errors.

These exercise the import-light functions only — no `mcp`, `starlette`,
`uvicorn`, or Hermes needed. The FastMCP/ASGI wiring is covered by the Modal
smoke test (`modal run src/flotta/worker/modal_app.py`).
"""

import time

from flotta.worker.config import WorkerConfig
from flotta.worker.server import _run_task_core, authorize, health_payload


def _cfg(**env):
    return WorkerConfig.from_env(env)


def _provider_cfg(**extra):
    base = {
        "FLOTTA_MODEL": "gpt-x",
        "FLOTTA_MODEL_BASE_URL": "https://api.example.com/v1",
        "FLOTTA_API_KEY": "sk-test",
    }
    base.update(extra)
    return WorkerConfig.from_env(base)


# -- authorize --------------------------------------------------------------


def test_authorize_open_when_no_token_configured():
    assert authorize(None, None) is True
    assert authorize(None, "Bearer anything") is True


def test_authorize_requires_header_when_token_set():
    assert authorize("secret", None) is False


def test_authorize_accepts_correct_bearer():
    assert authorize("secret", "Bearer secret") is True


def test_authorize_case_insensitive_scheme():
    assert authorize("secret", "bearer secret") is True


def test_authorize_rejects_wrong_token():
    assert authorize("secret", "Bearer nope") is False


def test_authorize_rejects_wrong_scheme():
    assert authorize("secret", "Basic secret") is False


def test_authorize_rejects_empty_token():
    assert authorize("secret", "Bearer ") is False


# -- health_payload ---------------------------------------------------------


def test_health_payload_without_provider():
    payload = health_payload(_cfg(FLOTTA_TIMEOUT_S="42"))
    assert payload["status"] == "ok"
    assert payload["service"] == "flotta-worker"
    assert payload["has_provider"] is False
    assert payload["timeout_s"] == 42
    assert payload["oneshot"] is False


def test_health_payload_with_provider():
    assert health_payload(_provider_cfg())["has_provider"] is True


# -- run_task core ----------------------------------------------------------


def test_run_task_missing_provider_returns_error():
    result = _run_task_core(_cfg(), "do it", 5)
    assert result["completed"] is False
    assert result["timed_out"] is False
    assert "missing provider config" in result["error"]


def test_run_task_success_maps_agent_result():
    class FakeAgent:
        def run_conversation(self, task, task_id=None):
            assert task == "summarize"
            assert task_id  # a task_id is always supplied
            return {"completed": True, "final_response": "done", "api_calls": 3}

    result = _run_task_core(_provider_cfg(), "summarize", 5, agent_factory=lambda cfg: FakeAgent())
    assert result["completed"] is True
    assert result["timed_out"] is False
    assert result["final_response"] == "done"
    assert result["api_calls"] == 3
    assert result["task_id"]


def test_run_task_honors_explicit_task_id():
    class FakeAgent:
        def run_conversation(self, task, task_id=None):
            return {"completed": True, "final_response": task_id}

    result = _run_task_core(
        _provider_cfg(), "t", 5, agent_factory=lambda cfg: FakeAgent(), task_id="fixed-id"
    )
    assert result["task_id"] == "fixed-id"
    assert result["final_response"] == "fixed-id"


def test_run_task_timeout():
    class SlowAgent:
        def run_conversation(self, task, task_id=None):
            time.sleep(5)
            return {"completed": True}

    result = _run_task_core(_provider_cfg(), "t", 0.2, agent_factory=lambda cfg: SlowAgent())
    assert result["completed"] is False
    assert result["timed_out"] is True
    assert "hard timeout" in result["error"]


def test_run_task_agent_exception_is_reported():
    def boom(cfg):
        raise RuntimeError("provider exploded")

    result = _run_task_core(_provider_cfg(), "t", 5, agent_factory=boom)
    assert result["completed"] is False
    assert result["timed_out"] is False
    assert "RuntimeError: provider exploded" in result["error"]


def test_run_task_agent_factory_receives_config():
    seen = {}

    class FakeAgent:
        def run_conversation(self, task, task_id=None):
            return {"completed": True}

    def factory(cfg):
        seen["model"] = cfg.model
        return FakeAgent()

    _run_task_core(_provider_cfg(), "t", 5, agent_factory=factory)
    assert seen["model"] == "gpt-x"


# -- bearer-auth ASGI wrapper -----------------------------------------------


def _drive_asgi(wrapped, scope):
    """Run an ASGI app once with a no-op receive; return (inner_hits, sent_msgs)."""
    import asyncio

    sent = []

    async def receive():
        return {"type": "http.request"}

    async def send(message):
        sent.append(message)

    asyncio.run(wrapped(scope, receive, send))
    return sent


def _wrap_with_spy(token):
    from flotta.worker.server import _bearer_auth_asgi

    hits = []

    async def inner(scope, receive, send):
        hits.append(scope["type"])

    return _bearer_auth_asgi(inner, token), hits


def test_bearer_auth_rejects_bad_token():
    wrapped, hits = _wrap_with_spy("secret")
    scope = {"type": "http", "headers": [(b"authorization", b"Bearer nope")]}
    sent = _drive_asgi(wrapped, scope)
    assert hits == []  # inner app never reached
    assert sent[0]["type"] == "http.response.start"
    assert sent[0]["status"] == 401


def test_bearer_auth_allows_good_token():
    wrapped, hits = _wrap_with_spy("secret")
    scope = {"type": "http", "headers": [(b"authorization", b"Bearer secret")]}
    sent = _drive_asgi(wrapped, scope)
    assert hits == ["http"]  # passed through to inner
    assert sent == []  # wrapper sent nothing itself


def test_bearer_auth_passes_lifespan_through_untouched():
    wrapped, hits = _wrap_with_spy("secret")
    sent = _drive_asgi(wrapped, {"type": "lifespan"})
    assert hits == ["lifespan"]  # session-manager lifespan is never gated
    assert sent == []


# -- entrypoint watchdog ----------------------------------------------------


def test_watchdog_fires_after_timeout():
    from flotta.worker.entrypoint import arm_watchdog

    fired = []
    thread = arm_watchdog(0.05, on_timeout=lambda: fired.append(True))
    thread.join(2.0)
    assert fired == [True]


def test_main_oneshot_without_task_returns_2():
    from flotta.worker import entrypoint

    # oneshot forced on, but no task -> exit code 2
    code = entrypoint.main({"FLOTTA_ONESHOT": "1", "HERMES_HOME": _tmp_home()})
    assert code == 2


def _tmp_home():
    import tempfile

    return tempfile.mkdtemp(prefix="flotta-hermes-")
