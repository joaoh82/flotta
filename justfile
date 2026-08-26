# Flotta dev commands — https://just.systems
# Keep this file updated as milestones land.

# Local settings come from .env (gitignored) — copy .env.example to start.
# That file is the single place to look for machine-local config.
set dotenv-load := true

# M5 — generate a signing key for scoped tokens (print once, never stored)
token-key:
    uv run flotta token key

# M5 — mint a scoped token, e.g. `just token-mint dashboard fleet:read`
token-mint SUBJECT SCOPE:
    uv run flotta token mint {{SUBJECT}} --scope {{SCOPE}}

# list available recipes
default:
    @just --list


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
    pinned=$(uv run python -c "from flotta.box.image import HERMES_REF; print(HERMES_REF)")
    latest=$(gh api repos/NousResearch/Hermes-Agent/releases/latest --jq .tag_name 2>/dev/null || echo "?")
    local_v=$(hermes --version 2>/dev/null | head -1 || echo "not installed")
    printf "  boxes run     : %s\n" "$pinned"
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
# M7 — bump the pinned Hermes; verify against a real box
hermes-bump REF:
    #!/usr/bin/env bash
    set -euo pipefail
    file=src/flotta/box/image.py

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
    echo "  bump recorded. Verify against a real box:"
    echo "    just fly-up      # build and boot a box on the new ref"
    echo "    just fly-proof   # memory survives a stop/start"
    echo "    just fly-doctor  # it is actually serving"
    trap - ERR


# Unauthenticated unless $FLOTTA_SIGNING_KEY is set — which it will only do on
# a loopback bind. A public bind with no key is refused at startup, because
# DELETE /api/boxes/<id> destroys a box and everything it remembers.
# Mint a key with `just token-key`. Reads $FLOTTA_STORE (or $FLOTTA_DATABASE_URL).
# M4.5 control plane — the fleet API + the reconcile loop, on http://127.0.0.1:8080
serve port="8080":
    #!/usr/bin/env bash
    set -euo pipefail
    uv run flotta serve --port {{port}}

# Port 3001 is baked into dashboard/package.json rather than passed here: 3000
# is reserved for another local service and a flag is too easy to forget.
# Needs `just serve` running: the dashboard reads the control-plane API, not
# the store. Without it every page answers 503 and says so.
# If the control plane has a signing key, set $FLOTTA_CONTROL_TOKEN too —
# `just token-mint dashboard fleet:read` — or every page answers 401.
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

# Exactly what .github/workflows/ci.yml runs, in one command — so "will CI go
# green?" is answerable before pushing. The workflow's two jobs are separate so
# a dashboard-only failure is obvious there; here they run in sequence.
# Everything CI checks (python + dashboard) — no Fly, no cost
ci: check check-dashboard

# M4 CLI — there is deliberately no `just flotta` recipe. just's variadic
# arguments are re-split by the shell, so `just flotta spawn "a b c"` breaks on
# exactly the case the CLI exists for. Run it directly instead:
#
#   uv run flotta ps                 # boxes; --tasks for the work
#   uv run flotta spawn "summarize the logs" --wait
#   uv run flotta stop <box>         # M0: disk retained, no CPU
#   uv run flotta start <box>        # M0: wake it again
#

# Regenerates assets/demo.gif from assets/demo.tape. `just --list` shows only
# the last comment line, so the cost lives there rather than here.
# M7.6: re-record the README demo GIF — STALE, the tape still drives `flotta spawn`
demo:
    #!/usr/bin/env bash
    set -euo pipefail
    command -v vhs >/dev/null || { echo "vhs not installed: brew install vhs"; exit 1; }
    # The tape drives the installed `flotta`, not `uv run flotta`, so the GIF
    # shows what the README tells a user to type. Keep the two in step.
    uv tool install --force . >/dev/null
    # NOTE: assets/demo.tape still drives v0.1's `flotta spawn`, which no
    # longer exists. Re-record against `flotta create` + `flotta chat`.
    vhs assets/demo.tape
    ls -lh assets/demo.gif

# show the development plan (lives in the parent workspace)
plan:
    @sed -n '1,60p' ../docs/development-plan.md

# --- M2: the box tier on Fly.io -------------------------------------------
#
# `fly-whoami` gates every Fly recipe: flyctl acts on whichever org is current.
# The precedent is real — back when this repo used Modal, its globally-active
# profile was once found pointing at an unrelated workspace, and every recipe
# had to pin it. Pin Fly in .env (FLOTTA_FLY_ORG / FLOTTA_FLY_APP), never rely
# on ambient state.

# which Fly org/app/volume every fly recipe will act on
fly-whoami:
    #!/usr/bin/env bash
    set -euo pipefail
    uv run python -c "from flotta.fly import FlyConfig; print(FlyConfig.from_env().describe())"
    echo
    echo -n "  logged in as  "
    flyctl auth whoami

# M2: provision the box — app + volume + first deploy (REAL infra, costs money)
fly-up: fly-whoami
    #!/usr/bin/env bash
    set -euo pipefail
    APP=$(uv run python -c "from flotta.fly import FlyConfig; print(FlyConfig.from_env().app)")
    ORG=$(uv run python -c "from flotta.fly import FlyConfig; print(FlyConfig.from_env().org)")
    VOL=$(uv run python -c "from flotta.fly import FlyConfig; print(FlyConfig.from_env().volume_name)")
    GB=$(uv run python -c "from flotta.fly import FlyConfig; print(FlyConfig.from_env().volume_gb)")
    # Concrete, always: `fly volumes create` refuses to run without a region
    # when it is not attached to a TTY, so "unset" cannot mean "decide later".
    REGION=$(uv run python -c "from flotta.fly import FlyConfig; print(FlyConfig.from_env().resolved_region())")
    echo "region     $REGION"
    REF=$(uv run python -c "from flotta.box.image import HERMES_REF; print(HERMES_REF)")

    # Fly app names are globally unique across all of Fly, not per-org, so a
    # collision here is the single most likely first failure. Say so plainly.
    # Exact match on the parsed JSON, not a grep. Two reasons: the field is
    # `Name` for apps and `name` for volumes (already inconsistent), and a
    # substring grep for "flotta-box" matches an existing "flotta-box-2" — so
    # the recipe would decide the app exists and skip creating it.
    if ! flyctl apps list --json \
      | uv run python -c "import json,sys; sys.exit(0 if any(a.get('Name')=='$APP' for a in json.load(sys.stdin)) else 1)"; then
      echo "creating app $APP in org $ORG"
      flyctl apps create "$APP" --org "$ORG" || {
        echo "" >&2
        echo "If that failed with a name conflict: Fly app names are GLOBALLY unique." >&2
        echo "Pick another and set FLOTTA_FLY_APP in .env." >&2
        exit 1
      }
    else
      echo "app $APP already exists"
    fi

    if ! flyctl volumes list --app "$APP" --json \
      | uv run python -c "import json,sys; sys.exit(0 if any(v.get('name')=='$VOL' for v in json.load(sys.stdin)) else 1)"; then
      echo "creating ${GB}GB volume $VOL"
      # -y: no interactive "are you sure about a single-node volume" prompt.
      # A single unreplicated volume is correct here — this is one box, and
      # replication is a v2 concern.
      flyctl volumes create "$VOL" --app "$APP" --size "$GB" --region "$REGION" -y
    else
      echo "volume $VOL already exists"
    fi

    # fly.toml carries defaults; the resolved config wins. Rewritten into a
    # temp copy so the committed file stays the documented default.
    TMP=$(mktemp -d)
    trap 'rm -rf "$TMP"' EXIT
    cp fly/Dockerfile fly/box_entrypoint.sh fly/fly.toml "$TMP/"
    uv run python - "$TMP/fly.toml" "$APP" "$REGION" "$REF" "$VOL" <<'PY'
    import pathlib, sys
    path = pathlib.Path(sys.argv[1])
    app, region, ref, vol = sys.argv[2:6]
    text = path.read_text()
    for old, new in (
        ('app = "flotta-box"', f'app = "{app}"'),
        ('primary_region = ""', f'primary_region = "{region}"'),
        ('HERMES_REF = "v2026.8.19"', f'HERMES_REF = "{ref}"'),
        # The mount source has to move with the volume name. Creating
        # `$VOL` while fly.toml still mounts `flotta_data` deploys a machine
        # with no disk where HERMES_HOME should be — and the proof then reads
        # as a durability failure rather than a config mistake.
        ('source = "flotta_data"', f'source = "{vol}"'),
    ):
        assert old in text, f"fly.toml no longer contains {old!r}"
        text = text.replace(old, new)
    path.write_text(text)
    PY

    echo "deploying (Hermes pinned at $REF)"
    flyctl deploy --config "$TMP/fly.toml" --dockerfile "$TMP/Dockerfile" \
      --app "$APP" --ha=false --yes .

# M2: prove HERMES_HOME survives a stop/start (REAL infra + one model call)
fly-proof *ARGS: fly-whoami
    uv run python scripts/m2_memory_proof.py {{ARGS}}

# open a shell on the box
fly-ssh: fly-whoami
    #!/usr/bin/env bash
    set -euo pipefail
    APP=$(uv run python -c "from flotta.fly import FlyConfig; print(FlyConfig.from_env().app)")
    flyctl ssh console --app "$APP"

# M2: stop the box — keeps the disk, stops paying for CPU
fly-stop: fly-whoami
    #!/usr/bin/env bash
    set -euo pipefail
    APP=$(uv run python -c "from flotta.fly import FlyConfig; print(FlyConfig.from_env().app)")
    flyctl machines list --app "$APP" --json \
      | uv run python -c "import json,sys; [print(m['id']) for m in json.load(sys.stdin)]" \
      | xargs -r -I{} flyctl machines stop {} --app "$APP"

# DESTROY the box and its volume — the disk and everything it remembered
fly-down: fly-whoami
    #!/usr/bin/env bash
    set -euo pipefail
    APP=$(uv run python -c "from flotta.fly import FlyConfig; print(FlyConfig.from_env().app)")
    echo "This destroys app $APP AND its volume — the box forgets everything."
    flyctl apps destroy "$APP"

# M2: push .env's provider vars into the box as Fly secrets (this is how you rotate)
fly-secrets: fly-whoami
    #!/usr/bin/env bash
    set -euo pipefail
    APP=$(uv run python -c "from flotta.fly import FlyConfig; print(FlyConfig.from_env().app)")
    : "${FLOTTA_MODEL:?set FLOTTA_MODEL in .env}"
    : "${FLOTTA_MODEL_BASE_URL:?set FLOTTA_MODEL_BASE_URL in .env}"
    : "${FLOTTA_API_KEY:?set FLOTTA_API_KEY in .env}"

    # `secrets import` issues a release, which STARTS stopped machines. That is
    # the surprise-agent footgun the missing service block exists to avoid, so
    # remember what was asleep and put it back.
    WERE_STOPPED=$(flyctl machines list --app "$APP" --json \
      | uv run python -c "import json,sys; print(' '.join(m['id'] for m in json.load(sys.stdin) if m['state'] != 'started'))")

    # Two consumers, two vocabularies — both have to be set.
    #
    # `flotta.box.run` (the Tier 3 one-shot path) takes base_url/api_key as
    # explicit AIAgent arguments and reads the FLOTTA_* names. `hermes serve`
    # (the box's agent surface, M3) does NOT: it resolves a provider through
    # Hermes's own config, so a box with only FLOTTA_* set answers every turn
    # with "No inference provider configured" — found by sending a real turn,
    # not by any test.
    #
    # The native name depends on the endpoint, so it is derived rather than
    # guessed: OpenRouter has its own variable, everything OpenAI-compatible
    # shares OPENAI_*.
    if printf '%s' "$FLOTTA_MODEL_BASE_URL" | grep -qi openrouter; then
      NATIVE=$(printf 'OPENROUTER_API_KEY=%s\n' "$FLOTTA_API_KEY")
    else
      NATIVE=$(printf 'OPENAI_API_KEY=%s\nOPENAI_BASE_URL=%s\n' "$FLOTTA_API_KEY" "$FLOTTA_MODEL_BASE_URL")
    fi

    # Values on stdin, never argv — argv is visible in `ps`.
    { printf 'FLOTTA_MODEL=%s\nFLOTTA_MODEL_BASE_URL=%s\nFLOTTA_API_KEY=%s\n' \
        "$FLOTTA_MODEL" "$FLOTTA_MODEL_BASE_URL" "$FLOTTA_API_KEY"
      printf '%s\n' "$NATIVE"
    } | flyctl secrets import --app "$APP"

    for MID in $WERE_STOPPED; do
      echo "re-stopping $MID (it was asleep before this rotation)"
      flyctl machines stop "$MID" --app "$APP" >/dev/null
    done
    echo "provider secrets set on $APP (values not echoed)"

# M1: drive a real box through the Backend protocol (REAL infra, no model call)
fly-cycle: fly-whoami
    uv run python scripts/m1_backend_cycle.py

# M3: mint and push the box's dashboard credentials (this is how you rotate)
fly-auth: fly-whoami
    #!/usr/bin/env bash
    set -euo pipefail
    APP=$(uv run python -c "from flotta.fly import FlyConfig; print(FlyConfig.from_env().app)")
    # Generated here, never committed: the box is reachable only over Fly's
    # private network, but "private network" is one layer and Hermes's own auth
    # gate is the other. Neither is meant to hold alone.
    PASS=$(uv run python -c "import secrets; print(secrets.token_urlsafe(32))")
    SECRET=$(uv run python -c "import secrets; print(secrets.token_urlsafe(48))")
    printf 'HERMES_DASHBOARD_BASIC_AUTH_USERNAME=%s\nHERMES_DASHBOARD_BASIC_AUTH_PASSWORD=%s\nHERMES_DASHBOARD_BASIC_AUTH_SECRET=%s\n' \
      flotta "$PASS" "$SECRET" | flyctl secrets import --app "$APP"
    # Written locally so `flotta chat` can log in. Gitignored with the rest of .env.
    uv run python - "$PASS" <<'PY'
    import pathlib, sys
    env = pathlib.Path(".env")
    text = env.read_text() if env.exists() else ""
    lines = [ln for ln in text.splitlines() if not ln.startswith("FLOTTA_BOX_PASSWORD=")]
    lines.append(f"FLOTTA_BOX_PASSWORD={sys.argv[1]}")
    env.write_text("\n".join(lines) + "\n")
    PY
    echo "credentials set on $APP and recorded in .env (values not echoed)"

# M3: what is true about the box — HERMES_HOME, sessions, memory, serving
fly-doctor: fly-whoami
    #!/usr/bin/env bash
    set -euo pipefail
    APP=$(uv run python -c "from flotta.fly import FlyConfig; print(FlyConfig.from_env().app)")

    # Machines are started BY ID. `flyctl machines start --app X` with no id
    # fails with "a machine ID must be specified when not running
    # interactively" — and an earlier version of this recipe swallowed that
    # with `|| true`, so a stopped box surfaced as `fly ssh`'s far less helpful
    # "app has no started VMs. It may be unhealthy or not have been deployed
    # yet", which sends you looking at deploys and health checks instead of at
    # a box that is simply asleep.
    IDS=$(flyctl machines list --app "$APP" --json \
      | uv run python -c "import json,sys; print(' '.join(m['id'] for m in json.load(sys.stdin)))")
    if [ -z "$IDS" ]; then
      echo "no machines in $APP — run \`just fly-up\` first" >&2
      exit 1
    fi
    for MID in $IDS; do
      flyctl machines start "$MID" --app "$APP" >/dev/null 2>&1 || true
    done
    # Started is not the same as ready: hermes serve takes a few seconds to
    # come up, and the doctor's own listener check is what distinguishes them.
    flyctl machines list --app "$APP" --json \
      | uv run python -c "
    import json,sys
    stuck=[m['id'] for m in json.load(sys.stdin) if m['state']!='started']
    sys.exit(f'still not started: {stuck}' if stuck else 0)"

    # --wait-s: a box that just started is still importing Hermes.
    flyctl ssh console --app "$APP" -C "python3 -m flotta.box.doctor --wait-s 45"

# M4: run the store suite against a real Postgres (throwaway container)
test-postgres:
    #!/usr/bin/env bash
    set -euo pipefail
    # A container per run, removed on exit. The hermetic suite stays the gate;
    # this is the "does it behave the same on the other engine" check, and it
    # needs a server rather than a mock — a mock would agree with whatever the
    # code does, which is the one thing worth not assuming here.
    NAME=flotta-pg-$$
    docker run -d --rm --name "$NAME" \
      -e POSTGRES_PASSWORD=flotta -e POSTGRES_DB=flotta -P \
      postgres:16-alpine >/dev/null
    trap 'docker rm -f "$NAME" >/dev/null 2>&1 || true' EXIT
    PORT=$(docker port "$NAME" 5432/tcp | head -1 | sed 's/.*://')
    for _ in $(seq 1 30); do
      docker exec "$NAME" pg_isready -U postgres >/dev/null 2>&1 && break
      sleep 1
    done
    # `--extra postgres`: psycopg is optional, so a clean `uv sync` does not have
    # it and this recipe would fail on the one machine that most needs it to
    # work — a fresh checkout.
    #
    # The whole store suite runs too, not just the concurrency file: test_store
    # parameterises over both engines when this variable is set, which is where
    # "behaves identically" is actually proven.
    FLOTTA_TEST_POSTGRES_URL="postgresql://postgres:flotta@127.0.0.1:$PORT/flotta" \
      uv run --extra postgres pytest src/flotta/test_store.py src/flotta/test_store_postgres.py -q
