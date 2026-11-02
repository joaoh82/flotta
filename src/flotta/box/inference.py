"""Tell Hermes which model this agent runs (FLOTTA-39).

Runs on the box, from `box_entrypoint.sh`, before `hermes serve` starts. Reads
`FLOTTA_MODEL`, `FLOTTA_MODEL_BASE_URL` and `FLOTTA_API_KEY` from the
environment — which the control plane writes, per agent — and settles the
`model:` block of `$HERMES_HOME/config.yaml` to match.

**Unlike `approvals.py`, this overwrites.** The approvals timeout is a default
the agent may change; the model is a choice the person made in the Flotta app,
and a machine that quietly kept an older one would be the window lying about
what the agent runs. Only the keys Flotta decides are touched
(`inference.MANAGED_KEYS`); anything else in the block is kept.

Every boot, not only the first: changing an agent's model writes the secret
and restarts the machine, and this is what turns that into Hermes's config.

See `flotta.inference` for why the file and not `HERMES_MODEL`, and for the
key leak the custom-endpoint arrangement closes.

**A conversation is told too (FLOTTA-112).** Hermes restores the route a conversation
last used, so once an agent changes provider, every older conversation still calls the
old one, whose key is gone. `unpin_sessions` writes to Hermes's own `state.db`, before
Hermes is running. That is a copy of `_stored_session_runtime_overrides` and
`session_gateway_runtime` as of v0.22.5, so re-check it at every Hermes bump.
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
from pathlib import Path
from typing import Any

from flotta.inference import (
    GATEWAY_PROVIDER,
    MANAGED_KEYS,
    gateway_provider,
    hermes_model_config,
    is_openrouter,
)


def settle_model(
    config: dict[str, Any] | None, wanted: dict[str, str]
) -> tuple[dict[str, Any], bool]:
    """The config with `model:` matching `wanted`, and whether it changed.

    Pure. A `model:` that is a bare string is how Hermes writes a model with no
    provider; it becomes a block, as Hermes itself does when a sub-key is set.
    A managed key that `wanted` does not mention is removed — switching from a
    custom endpoint to OpenRouter must not leave the old key on disk.
    """
    config = dict(config or {})
    current = config.get("model")
    if isinstance(current, str):
        block: dict[str, Any] = {"default": current} if current else {}
    elif isinstance(current, dict):
        block = dict(current)
    elif current is None:
        block = {}
    else:
        # Something Hermes would not read either. Replacing it is the only way
        # to get a working agent, and there is nothing to preserve.
        block = {}

    settled = {k: v for k, v in block.items() if k not in MANAGED_KEYS}
    settled.update(wanted)
    if settled == current:
        return config, False
    config["model"] = settled
    return config, True


def settle_gateway(config: dict[str, Any], wanted: dict[str, Any] | None) -> dict[str, Any]:
    """The config with Flotta's `providers:` entry as `wanted`, or gone. Pure.

    Only that entry: any other provider is whoever set it's.
    """
    config = dict(config)
    current = config.get("providers")
    providers = dict(current) if isinstance(current, dict) else {}
    providers.pop(GATEWAY_PROVIDER, None)
    if wanted is not None:
        providers[GATEWAY_PROVIDER] = wanted
    if providers:
        config["providers"] = providers
    else:
        config.pop("providers", None)
    return config


def apply(home: Path, env: dict[str, str] | None = None) -> str:
    """Settle `<home>/config.yaml`. Returns a boot-log line; never raises.

    A box that cannot write its model config must still boot — it then runs
    whatever Hermes already had, which is a wrong model rather than no agent.
    The line says so, and never contains the key.
    """
    import yaml

    source = os.environ if env is None else env
    model = (source.get("FLOTTA_MODEL") or "").strip()
    base_url = (source.get("FLOTTA_MODEL_BASE_URL") or "").strip()
    api_key = (source.get("FLOTTA_API_KEY") or "").strip()
    if not (model and base_url and api_key):
        return "model left alone: FLOTTA_MODEL / FLOTTA_MODEL_BASE_URL / FLOTTA_API_KEY not all set"

    wanted = hermes_model_config(model, base_url, api_key)
    path = home / "config.yaml"
    try:
        loaded = yaml.safe_load(path.read_text(encoding="utf-8")) if path.exists() else {}
    except (OSError, yaml.YAMLError) as exc:
        return f"model left alone: could not read {path}: {exc}"
    if loaded is not None and not isinstance(loaded, dict):
        return f"model left alone: {path} is not a mapping"

    settled, changed = settle_model(loaded, wanted)
    # Only for a gateway in front of OpenRouter: see `gateway_provider`.
    through = (source.get("FLOTTA_GATEWAY_UPSTREAM") or "").strip() == "openrouter"
    settled = settle_gateway(settled, gateway_provider(model, base_url) if through else None)
    changed = changed or settled != (loaded or {})
    described = f"{model} via {wanted['provider']}"
    if not changed:
        return f"model: already {described}"
    try:
        path.write_text(yaml.safe_dump(settled, sort_keys=False), encoding="utf-8")
        if "api_key" in wanted:
            # The file now holds a credential. Hermes runs as the same user,
            # so this does not hide it from the agent — it keeps it out of
            # anything that reads the volume as someone else.
            path.chmod(0o600)
    except OSError as exc:
        return f"model left alone: could not write {path}: {exc}"
    return f"model: {described}"


#: Where a conversation's route is kept in its `model_config`.
_ROUTE_KEYS = ("provider", "base_url", "api_mode", "gateway_runtime")


def _stored_route(config: dict[str, Any], billing: str, billing_url: str) -> tuple[str, str]:
    """A session row's provider and URL, in the order Hermes reads them."""
    nested = config.get("gateway_runtime")
    if isinstance(nested, dict) and nested.get("provider"):
        return str(nested["provider"]), str(nested.get("base_url") or "")
    provider = str(config.get("provider") or "") or billing
    return provider, str(config.get("base_url") or "") or billing_url


def _stranded(provider: str, url: str, wanted: dict[str, str]) -> bool:
    """Whether a stored route goes somewhere this agent has moved off.

    Only OpenRouter and custom endpoints, the two Flotta moves an agent between. Any
    other provider is somebody's own choice. `auto` is one Hermes already routes by
    the config, and a custom route with no URL names an entry the config still has.
    """
    name = provider.strip().lower()
    leaving = wanted["provider"] == "openrouter"
    if name == "openrouter" or is_openrouter(url):
        return not leaving
    if name == "custom" or name.startswith("custom:"):
        return leaving or (bool(url) and url.rstrip("/") != wanted["base_url"].rstrip("/"))
    return False


def unpin_sessions(home: Path, env: dict[str, str] | None = None) -> str:
    """Clear stored routes this agent has moved off, so they follow `config.yaml`.

    The model a conversation was using stays its own. Returns a boot-log line, or ""
    with nothing to say; never raises, because a box must boot whatever this finds.
    """
    source = os.environ if env is None else env
    model = (source.get("FLOTTA_MODEL") or "").strip()
    base_url = (source.get("FLOTTA_MODEL_BASE_URL") or "").strip()
    api_key = (source.get("FLOTTA_API_KEY") or "").strip()
    path = home / "state.db"
    if not (model and base_url and api_key and path.exists()):
        return ""
    wanted = hermes_model_config(model, base_url, api_key)
    moved = 0
    try:
        conn = sqlite3.connect(path, timeout=5.0)
        try:
            rows = conn.execute(
                "SELECT id, model_config, billing_provider, billing_base_url FROM sessions"
            ).fetchall()
            with conn:
                for session, raw, billing, billing_url in rows:
                    try:
                        parsed = json.loads(raw) if raw else {}
                    except (ValueError, TypeError):
                        parsed = None
                    config = parsed if isinstance(parsed, dict) else {}
                    if not _stranded(
                        *_stored_route(config, billing or "", billing_url or ""), wanted
                    ):
                        continue
                    # `billing_provider` too: Hermes routes by it, and fills it in again
                    # on the conversation's next call.
                    kept = {k: v for k, v in config.items() if k not in _ROUTE_KEYS}
                    conn.execute(
                        "UPDATE sessions SET model_config = ?, billing_provider = NULL, "
                        "billing_base_url = NULL WHERE id = ?",
                        (raw if parsed is None else (json.dumps(kept) if kept else None), session),
                    )
                    moved += 1
        finally:
            conn.close()
    except sqlite3.Error as exc:
        return f"conversations left alone: could not update {path}: {exc}"
    if not moved:
        return ""
    return f"{moved} conversation(s) now follow this agent's provider"


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    home = Path(args[0]) if args else Path("/data/hermes")
    print(f"[flotta-box] {apply(home)}")
    moved = unpin_sessions(home)
    if moved:
        print(f"[flotta-box] {moved}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
