"""Which model an agent runs, and how Hermes is told (FLOTTA-39).

Pure, and importable on a box: the control plane uses it to decide what to
write, and `flotta.box.inference` uses it at boot to turn what was written into
something Hermes reads.

## Hermes never read `FLOTTA_MODEL`

Found starting this ticket, by reading `hermes serve`'s model resolution at the
tag the boxes run (v2026.9.14): the model comes from a per-session override,
then `HERMES_MODEL` / `HERMES_INFERENCE_MODEL`, then `config.yaml`'s
`model.default`, then a built-in silent default — **`z-ai/glm-5.2`**. The fleet
default was also `z-ai/glm-5.2`, so every agent ran the configured model by
coincidence. Changing the fleet setting would have changed nothing.

## Why `config.yaml` and not `HERMES_MODEL`

- Hermes calls `config.yaml` "the single source of truth for endpoint URLs",
  and a custom endpoint's key is only read from there — see below.
- It is re-read for every new session, and an open session that has not
  pinned a model follows it at the start of its next turn.
- An environment variable would beat the file, so a person choosing a model
  in Hermes's own settings would be silently overridden with no way to see why.

## The leak this also closes

`fleet_secrets` handed a non-OpenRouter endpoint to Hermes as
`OPENAI_API_KEY` + `OPENAI_BASE_URL`. With `provider: auto`, Hermes resolves
either key to **OpenRouter** and ignores `OPENAI_BASE_URL`, so a self-hosted
proxy's key would have been sent to openrouter.ai. For a custom host Hermes
also refuses to send `OPENAI_API_KEY` at all (a deliberate leak guard, matched
on host). The only configuration that works is `provider: custom` with
`base_url` and `api_key` in the file — which is what `hermes_model_config`
writes.
"""

from __future__ import annotations

import re
from urllib.parse import urlparse

#: A model id as OpenRouter and most gateways spell it: `vendor/name`,
#: sometimes with a `:variant`. Deliberately loose on characters, strict on
#: shape — whitespace or a quote is a typo, not a model.
_MODEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._\-/:@+]{0,199}$")


class InvalidModel(ValueError):
    """A model name that cannot be one."""


def validate_model(model: str | None) -> str:
    """Normalise a model id, or raise. Empty is refused: "no override" is None."""
    value = (model or "").strip()
    if not value:
        raise InvalidModel("which model? Leave it unset to use the fleet default.")
    if not _MODEL.match(value):
        raise InvalidModel(
            f"{value!r} is not a model id. Use the provider's id, like "
            "'anthropic/claude-sonnet-4.5' or 'z-ai/glm-5.2'."
        )
    return value


def is_openrouter(base_url: str) -> bool:
    """By **host**, never substring: `https://proxy.internal/openrouter/v1` is
    not OpenRouter, and treating it as one sends its traffic elsewhere."""
    host = (urlparse(base_url).hostname or "").lower()
    return host == "openrouter.ai" or host.endswith(".openrouter.ai")


def provider_secrets(model: str, base_url: str, api_key: str) -> dict[str, str]:
    """What a box is given so its boot step can configure Hermes.

    The `FLOTTA_*` names are the source the boot step reads. `OPENROUTER_API_KEY`
    is kept for OpenRouter because Hermes looks it up by that name even when
    the file names the provider. Nothing else: `OPENAI_BASE_URL` was set here
    for years and Hermes never read it on this path.
    """
    secrets = {
        "FLOTTA_MODEL": model,
        "FLOTTA_MODEL_BASE_URL": base_url,
        "FLOTTA_API_KEY": api_key,
    }
    if is_openrouter(base_url):
        secrets["OPENROUTER_API_KEY"] = api_key
    return secrets


def hermes_model_config(model: str, base_url: str, api_key: str) -> dict[str, str]:
    """The `model:` block Hermes should have, for these three values.

    OpenRouter: the provider by name and the model; the key is found by its
    env name, so it is **not** written to the file. Anything else:
    `provider: custom` with the endpoint and the key, because that is the only
    arrangement in which Hermes sends a key to a non-OpenRouter host.
    """
    if is_openrouter(base_url):
        return {"default": model, "provider": "openrouter", "base_url": base_url}
    return {"default": model, "provider": "custom", "base_url": base_url, "api_key": api_key}


#: Keys in `model:` that Flotta decides. Anything else in the block — an
#: `api_mode`, a context length — belongs to whoever set it and is kept.
MANAGED_KEYS = ("default", "provider", "base_url", "api_key")
