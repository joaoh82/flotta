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

# Compares the Hermes your workers run, the latest upstream release, and the
# Hermes running locally as orchestrator. There is no longer an "installable
# ceiling": the image clones and installs editable, so any tag is reachable.
# M7 — report Hermes version drift (pinned vs. latest vs. local)
hermes-check:
    #!/usr/bin/env bash
    set -euo pipefail
    pinned=$(uv run python -c "from flotta.worker.image import HERMES_REF; print(HERMES_REF)")
    latest=$(gh api repos/NousResearch/Hermes-Agent/releases/latest --jq .tag_name 2>/dev/null || echo "?")
    local_v=$(hermes --version 2>/dev/null | head -1 || echo "not installed")
    printf "  workers run   : %s\n" "$pinned"
    printf "  latest release: %s\n" "$latest"
    printf "  your local    : %s\n" "$local_v"
    echo
    if [ "$pinned" = "$latest" ]; then
      echo "  Up to date."
    elif [ "$latest" = "?" ]; then
      echo "  Could not reach GitHub; pin unchanged." >&2
    else
      echo "  BEHIND. 'just hermes-bump $latest' to move (rebuilds and re-verifies live)."
    fi

# Bumping is not mechanical: SEAM_NOTES' headless boot recipe was validated
# against one version, so this rebuilds the image and re-runs the live checks.
#
# On failure it **restores the previous pin**. An earlier version left the
# broken edit in place "so you can inspect it", which in practice meant a
# working tree pinned to a Hermes that cannot build — on `main`, where the
# next commit would have shipped it.
# M7 — bump the pinned Hermes and re-verify against real Modal
hermes-bump REF:
    #!/usr/bin/env bash
    set -euo pipefail
    file=src/flotta/worker/image.py

    branch=$(git branch --show-current)
    if [ "$branch" = "main" ]; then
      echo "ERROR: refusing to edit the pin on 'main'." >&2
      echo "  git switch -c chore/hermes-$(echo '{{REF}}' | tr -d 'v.')" >&2
      exit 1
    fi
    if ! git diff --quiet -- "$file"; then
      echo "ERROR: $file has uncommitted changes; commit or revert first." >&2
      exit 1
    fi

    previous=$(grep -oE 'DEFAULT_HERMES_REF = "[^"]+"' "$file" | cut -d'"' -f2)

    restore() {
      git checkout -- "$file"
      echo "  restored the previous pin ($previous) — nothing was left broken." >&2
    }
    trap 'restore' ERR

    sed -i.bak -E 's|DEFAULT_HERMES_REF = "[^"]+"|DEFAULT_HERMES_REF = "{{REF}}"|' "$file" && rm -f "$file.bak"
    echo "  $previous -> {{REF}}"
    echo "  rebuilding and re-verifying (smoke, then a live task)..."
    just smoke
    just deploy
    just e2e-live
    trap - ERR
    echo "  {{REF}} verified: image builds, MCP answers, a real task round-trips."

# M2 worker smoke test — build image on Modal, confirm the MCP endpoint answers (hermetic, no API key)
smoke: modal-whoami
    MODAL_PROFILE={{modal_profile}} modal run src/flotta/worker/modal_app.py

# Creates the named provider secret only if it is absent — never overwrites, so a
# deploy cannot clobber a key you rotated in the Modal dashboard or via secret-sync.
# M3 — ensure the provider secret exists (empty is fine; dry-run needs no provider)
secret-ensure: modal-whoami
    #!/usr/bin/env bash
    set -euo pipefail
    if MODAL_PROFILE={{modal_profile}} modal secret list 2>/dev/null | grep -q ' flotta-provider '; then
      echo "secret 'flotta-provider' exists — leaving it alone"
    else
      echo "creating empty secret 'flotta-provider' (use 'just secret-sync' to fill it)"
      MODAL_PROFILE={{modal_profile}} modal secret create flotta-provider FLOTTA_PLACEHOLDER=unset
    fi

# Rotate a key: edit .env, run this. No code change and no redeploy — the named
# secret is the container's source of truth (M7.1a).
#
# It is NOT instant, and the reason is worth knowing: a secret is injected as
# environment variables when a container *starts*, so any container Modal is
# still keeping warm goes on serving the old value until it scales down. New
# containers get the new value immediately. To force it, stop the app:
#   modal app stop flotta-provision -y
# M7 — push local provider credentials into the named Modal secret
secret-sync: modal-whoami
    #!/usr/bin/env bash
    set -euo pipefail
    missing=()
    for k in FLOTTA_MODEL FLOTTA_MODEL_BASE_URL FLOTTA_API_KEY; do
      [ -n "${!k:-}" ] || missing+=("$k")
    done
    if [ ${#missing[@]} -gt 0 ]; then
      echo "ERROR: not set in .env: ${missing[*]}" >&2
      echo "Fill them in .env (see .env.example), then re-run." >&2
      exit 1
    fi
    MODAL_PROFILE={{modal_profile}} modal secret create flotta-provider --force \
      FLOTTA_MODEL="$FLOTTA_MODEL" \
      FLOTTA_MODEL_BASE_URL="$FLOTTA_MODEL_BASE_URL" \
      FLOTTA_API_KEY="$FLOTTA_API_KEY"
    echo "secret updated — no redeploy needed."
    echo "New containers use it immediately; a warm one may serve the old value"
    echo "until it scales down. To force it now: modal app stop flotta-provision -y"

# M3 — deploy the provisioning app (run_worker). Required before `just e2e`.
deploy: modal-whoami secret-ensure
    MODAL_PROFILE={{modal_profile}} modal deploy src/flotta/provision.py

# `just --list` shows only the LAST comment line, so keep that line a complete
# summary — earlier lines are detail for anyone reading the file.
# Spawn -> watch -> teardown against real Modal, asserting the store at each step.
# M3 end-to-end lifecycle, dry-run by default (no LLM, no provider key needed)
e2e *ARGS: modal-whoami
    MODAL_PROFILE={{modal_profile}} uv run python scripts/e2e_lifecycle.py {{ARGS}}

# same, but with a real Hermes task — needs FLOTTA_MODEL/FLOTTA_MODEL_BASE_URL/FLOTTA_API_KEY
e2e-live: (e2e "--live")

# Symlinked rather than copied, so edits to the skill take effect immediately
# without a reinstall. Remove with `just uninstall-skill`.
# M6 — install the orchestrator skill into the local Hermes (~/.hermes/skills)
install-skill:
    #!/usr/bin/env bash
    set -euo pipefail
    dest="${HERMES_HOME:-$HOME/.hermes}/skills/flotta-orchestrator"
    if [ ! -d "$(dirname "$dest")" ]; then
      echo "ERROR: no Hermes skills directory at $(dirname "$dest")." >&2
      echo "Is Hermes installed? Set HERMES_HOME if it lives elsewhere." >&2
      exit 1
    fi
    # Refuse to clobber a real directory — only ever replace our own symlink.
    if [ -e "$dest" ] && [ ! -L "$dest" ]; then
      echo "ERROR: $dest exists and is not a symlink; refusing to overwrite." >&2
      exit 1
    fi
    ln -sfn "$(pwd)/skills/orchestrator" "$dest"
    echo "linked $dest -> $(pwd)/skills/orchestrator"

# M6 — remove the orchestrator skill from the local Hermes
uninstall-skill:
    #!/usr/bin/env bash
    set -euo pipefail
    dest="${HERMES_HOME:-$HOME/.hermes}/skills/flotta-orchestrator"
    if [ -L "$dest" ]; then rm "$dest" && echo "removed $dest"
    elif [ -e "$dest" ]; then echo "$dest is not a symlink; leaving it alone." >&2; exit 1
    else echo "nothing installed at $dest"; fi

# Port 3001 is baked into dashboard/package.json rather than passed here: 3000
# is reserved for another local service and a flag is too easy to forget.
# Reads $FLOTTA_STORE, else ../fleet.db — the same store the CLI writes.
# M5 dashboard — local fleet view on http://localhost:3001
dashboard:
    #!/usr/bin/env bash
    set -euo pipefail
    cd dashboard
    [ -d node_modules ] || npm install
    npm run dev

# type-check + lint the dashboard (the Python `check` recipe does not cover it)
check-dashboard:
    #!/usr/bin/env bash
    set -euo pipefail
    cd dashboard
    [ -d node_modules ] || npm install
    npx tsc --noEmit
    npx eslint .

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
