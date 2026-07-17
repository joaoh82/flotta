# Flotta dev commands — https://just.systems
# Keep this file updated as milestones land (M2: modal smoke test, M4: CLI, M5: dashboard).

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

# show the development plan (lives in the parent workspace)
plan:
    @sed -n '1,60p' ../docs/development-plan.md
