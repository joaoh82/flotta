# Flotta dev commands — https://just.systems
# Keep this file updated as milestones land (M2: modal smoke test, M4: CLI, M5: dashboard).

# Local settings come from .env (gitignored) — copy .env.example to start.
# That file is the single place to look for machine-local config.
set dotenv-load := true

# Modal profile for THIS project. Every modal recipe pins it explicitly so no
# Flotta command ever inherits whatever `modal profile activate` left global —
# a wrong active profile would otherwise build/deploy into an unrelated
# workspace. Create it with:
#   modal token new --profile flotta --no-activate
# (switch to the Flotta workspace in the Modal dashboard first — the token is
# minted for the workspace your browser session is in). Override per-shell with
# FLOTTA_MODAL_PROFILE=<name>.
modal_profile := env_var_or_default("FLOTTA_MODAL_PROFILE", "flotta")

# list available recipes
default:
    @just --list

# verify + print which Modal workspace the flotta recipes target (fails if unauthenticated)
modal-whoami:
    #!/usr/bin/env bash
    set -euo pipefail
    # `modal profile current` only echoes $MODAL_PROFILE back and never validates,
    # so authenticate for real: `modal app list` fails on a missing/bad profile.
    if ! MODAL_PROFILE={{modal_profile}} modal app list >/dev/null 2>&1; then
      echo "ERROR: Modal profile '{{modal_profile}}' is missing or not authenticated." >&2
      echo "Switch to the Flotta workspace in the Modal dashboard, then run:" >&2
      echo "  modal token new --profile {{modal_profile}} --no-activate" >&2
      exit 1
    fi
    echo "Modal target for flotta recipes:"
    MODAL_PROFILE={{modal_profile}} modal profile list | grep '•'

# run the test suite
test *ARGS:
    uv run pytest {{ARGS}}

# run a single test by keyword, e.g. `just test-one transition`
test-one K:
    uv run pytest -k "{{K}}"

# lint
lint:
    uv run ruff check src

# auto-format (and fix imports)
fmt:
    uv run ruff format src
    uv run ruff check --fix src

# lint + tests — run before committing
check: lint test

# M2 worker smoke test — build image on Modal, confirm the MCP endpoint answers (hermetic, no API key)
smoke: modal-whoami
    MODAL_PROFILE={{modal_profile}} modal run src/flotta/worker/modal_app.py

# M3 — deploy the provisioning app (run_worker). Required before `just e2e`.
deploy: modal-whoami
    MODAL_PROFILE={{modal_profile}} modal deploy src/flotta/provision.py

# M3 end-to-end lifecycle against real Modal: spawn -> watch -> teardown, asserting
# the store at each step. Dry-run by default (no LLM, no provider key needed).
e2e *ARGS: modal-whoami
    MODAL_PROFILE={{modal_profile}} uv run python scripts/e2e_lifecycle.py {{ARGS}}

# same, but with a real Hermes task — needs FLOTTA_MODEL/FLOTTA_MODEL_BASE_URL/FLOTTA_API_KEY
e2e-live: (e2e "--live")

# M4 CLI — there is deliberately no `just flotta` recipe. just's variadic
# arguments are re-split by the shell, so `just flotta spawn "a b c"` breaks on
# exactly the case the CLI exists for. Run it directly instead:
#
#   uv run flotta ps
#   uv run flotta spawn "summarize the logs" --wait
#
# The workspace no longer needs pinning at the call site: the CLI resolves
# FLOTTA_MODAL_PROFILE itself (env, then .env) before touching Modal, so an
# installed bare `flotta` targets the right workspace on its own.

# show the development plan (lives in the parent workspace)
plan:
    @sed -n '1,60p' ../docs/development-plan.md
