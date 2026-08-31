#!/usr/bin/env bash
# `gh`, wrapped so it authenticates the way the box does.
#
# ## Why shadow `gh` rather than ship a `flotta-gh`
#
# `gh` has no credential-helper hook. It reads `$GH_TOKEN`, or a token it
# stored in `hosts.yml` — and writing the fleet's token into `hosts.yml` is
# exactly the long-lived-credential-at-rest that `git-credential-flotta`
# exists to avoid.
#
# So something has to fetch a short-lived token and put it in `gh`'s
# environment for one invocation. That something could be called `flotta-gh`,
# and then every agent that has ever read a README would type `gh` and get
# "not logged into any GitHub hosts" instead. The image already made this
# trade once — `fd` is a symlink to `fdfind` because every model's training
# data says `fd` — and the same reasoning applies with more force here,
# because the failure is silent-ish rather than a missing binary.
#
# The shadow is only defensible if it is invisible, so: it prints nothing on
# success, changes no arguments, and in every case it cannot help it `exec`s
# the real `gh` unchanged. `gh --version` is `gh --version`.
#
# ## What it does not fix
#
# The token lands in the real `gh`'s environment, where the agent could read
# it from `/proc`. That is not a new hole — the agent may invoke the credential
# helper directly, and must be able to, or it could not push. The property
# being kept is that no GitHub credential is *at rest* on the box and that
# every one it gets is scoped to a repository the box was granted.
set -uo pipefail

REAL_GH=/usr/bin/gh
if [ ! -x "$REAL_GH" ]; then
  echo "flotta: $REAL_GH is missing — the gh shim has nothing to wrap" >&2
  exit 127
fi

# Something already authenticated this shell. Its choice wins.
if [ -n "${GH_TOKEN:-}" ] || [ -n "${GITHUB_TOKEN:-}" ]; then
  exec "$REAL_GH" "$@"
fi

# A control-plane round trip is only worth it inside a repository that lives on
# GitHub. Outside one — `gh --help`, `gh api` in /tmp — pass straight through.
remote=$(git config --get remote.origin.url 2>/dev/null || true)
case "$remote" in
  *github.com[:/]*) ;;
  *) exec "$REAL_GH" "$@" ;;
esac

path=${remote#*github.com}
path=${path#[:/]}
path=${path%.git}
path=${path%/}

# `git credential fill` rather than calling the helper directly: it goes
# through git's own configuration, so this cannot drift from what `git push`
# does. If the two ever disagreed, `gh pr create` would succeed against a
# repository `git push` had just refused.
credential=$(
  printf 'protocol=https\nhost=github.com\npath=%s\n\n' "$path" \
    | GIT_TERMINAL_PROMPT=0 git credential fill 2>/dev/null || true
)
token=$(printf '%s\n' "$credential" | sed -n 's/^password=//p' | head -1)

# No credential is not an error here. `gh` says "not logged in" far better than
# this script could, and for a public repo some subcommands work regardless.
if [ -z "$token" ]; then
  exec "$REAL_GH" "$@"
fi

exec env GH_TOKEN="$token" "$REAL_GH" "$@"
