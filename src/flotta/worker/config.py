"""Worker configuration parsed from the container environment.

The worker's whole contract with the outside world is a handful of env vars
(see the table below). `WorkerConfig.from_env` is the single place that reads
them, so it is pure, validated, and cheap to unit-test.

Env contract (all optional unless a task actually runs):

| Var                     | Default        | Meaning                                        |
|-------------------------|----------------|------------------------------------------------|
| `FLOTTA_TASK`           | (unset)        | The task prompt. Presence flips mode to one-shot. |
| `FLOTTA_TIMEOUT_S`      | `900`          | Hard lifetime/task timeout in seconds (M2.3).  |
| `FLOTTA_ONESHOT`        | auto           | `1`=run-once, `0`=serve; default once iff task set. |
| `HERMES_HOME`           | `/tmp/hermes`  | Writable, ephemeral Hermes store (SEAM_NOTES Q3). |
| `FLOTTA_HOST`           | `127.0.0.1`    | MCP bind host.                                 |
| `FLOTTA_PORT`           | `8080`         | MCP bind port (never 3000 — reserved).         |
| `FLOTTA_AUTH_TOKEN`     | (unset)        | Bearer token; when unset the server is open.   |
| `FLOTTA_MODEL`          | `""`           | Model id for the pinned provider.              |
| `FLOTTA_MODEL_BASE_URL` | (unset)        | Provider base URL (falls back to OPENAI_BASE_URL). |
| `FLOTTA_API_KEY`        | (unset)        | Provider key (falls back to OPENAI_API_KEY).   |
| `FLOTTA_TOOLSETS`       | (unset → None) | CSV of Hermes toolsets; None = Hermes default. |
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass

DEFAULT_TIMEOUT_S = 900
DEFAULT_HERMES_HOME = "/tmp/hermes"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8080

_TRUTHY = {"1", "true", "yes", "on"}
_FALSY = {"0", "false", "no", "off"}


def _parse_bool(value: str | None, *, default: bool) -> bool:
    if value is None:
        return default
    v = value.strip().lower()
    if v in _TRUTHY:
        return True
    if v in _FALSY:
        return False
    raise ValueError(f"expected a boolean, got {value!r}")


def _parse_positive_int(value: str | None, *, name: str, default: int) -> int:
    if value is None or value.strip() == "":
        return default
    try:
        n = int(value.strip())
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer, got {value!r}") from exc
    if n <= 0:
        raise ValueError(f"{name} must be positive, got {n}")
    return n


def _clean(value: str | None) -> str | None:
    if value is None:
        return None
    v = value.strip()
    return v or None


@dataclass(frozen=True)
class WorkerConfig:
    """Immutable snapshot of the worker's runtime configuration."""

    task: str | None
    timeout_s: int
    oneshot: bool
    hermes_home: str
    host: str
    port: int
    auth_token: str | None
    base_url: str | None
    api_key: str | None
    model: str
    toolsets: list[str] | None

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> WorkerConfig:
        env = os.environ if env is None else env

        task = _clean(env.get("FLOTTA_TASK"))
        timeout_s = _parse_positive_int(
            env.get("FLOTTA_TIMEOUT_S"), name="FLOTTA_TIMEOUT_S", default=DEFAULT_TIMEOUT_S
        )
        # Default: run-once when handed a task, otherwise serve until torn down.
        oneshot = _parse_bool(env.get("FLOTTA_ONESHOT"), default=task is not None)
        hermes_home = _clean(env.get("HERMES_HOME")) or DEFAULT_HERMES_HOME
        host = _clean(env.get("FLOTTA_HOST")) or DEFAULT_HOST
        port = _parse_positive_int(env.get("FLOTTA_PORT"), name="FLOTTA_PORT", default=DEFAULT_PORT)
        auth_token = _clean(env.get("FLOTTA_AUTH_TOKEN"))

        base_url = _clean(env.get("FLOTTA_MODEL_BASE_URL")) or _clean(env.get("OPENAI_BASE_URL"))
        api_key = _clean(env.get("FLOTTA_API_KEY")) or _clean(env.get("OPENAI_API_KEY"))
        model = _clean(env.get("FLOTTA_MODEL")) or ""

        raw_toolsets = _clean(env.get("FLOTTA_TOOLSETS"))
        toolsets = (
            [t.strip() for t in raw_toolsets.split(",") if t.strip()] if raw_toolsets else None
        )

        return cls(
            task=task,
            timeout_s=timeout_s,
            oneshot=oneshot,
            hermes_home=hermes_home,
            host=host,
            port=port,
            auth_token=auth_token,
            base_url=base_url,
            api_key=api_key,
            model=model,
            toolsets=toolsets,
        )

    def provider_missing(self) -> str | None:
        """Return a human-readable reason if the provider config is incomplete.

        Kept separate from `from_env` so the server can boot and answer
        `health` without a provider (the hermetic smoke path); only running an
        actual task requires the provider to be fully configured.
        """
        missing = [
            name
            for name, value in (
                ("FLOTTA_MODEL", self.model),
                ("FLOTTA_MODEL_BASE_URL", self.base_url),
                ("FLOTTA_API_KEY", self.api_key),
            )
            if not value
        ]
        if missing:
            return "missing provider config: " + ", ".join(missing)
        return None

    @property
    def mcp_url(self) -> str:
        """The streamable-http endpoint the orchestrator dials."""
        return f"http://{self.host}:{self.port}/mcp"
