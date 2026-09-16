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
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

from flotta.inference import MANAGED_KEYS, hermes_model_config


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


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    home = Path(args[0]) if args else Path("/data/hermes")
    print(f"[flotta-box] {apply(home)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
