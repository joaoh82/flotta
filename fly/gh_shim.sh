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
# ## Pull requests, the two exceptions (FLOTTA-171)
#
# `gh pr merge` asks the agency first (`flotta-pr may-merge`) and is refused
# unless the agency lets this agent merge its own pull requests, or when it
# cannot be asked. That is a guard against an agent merging by mistake, or
# because someone in a conversation told it to, and not a wall: the agent has
# root, `/usr/bin/gh` is right there, and so is the REST API.
#
# `gh pr create` that succeeds is followed by `flotta-pr report <url>`, so the
# agency sees the pull request and its reviews come back to the agent: on every
# path, with a token of the shell's own or the one fetched here. It
# prints nothing when that works and one `flotta:` line when it does not; the
# pull request is made either way, and `gh`'s exit status is the shim's.
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

# `gh pr merge`, unless asking how to use it: the agency's to allow.
if [ "${1:-}" = pr ] && [ "${2:-}" = merge ]; then
  wants_help=0
  for arg in "$@"; do
    case "$arg" in -h | --help) wants_help=1 ;; esac
  done
  if [ "$wants_help" -eq 0 ]; then
    if ! answer=$(flotta-pr may-merge 2>&1); then
      echo "flotta: gh pr merge refused. ${answer:-The agency could not be asked whether this agent may merge.}" >&2
      exit 1
    fi
  fi
fi

# `gh` as asked, with `token` in its environment when there is one. Every path
# below ends here, so a `gh pr create` that succeeds is reported however this
# shell was authenticated.
run_gh() {
  local token=$1
  shift
  local -a with=()
  if [ -n "$token" ]; then with=("GH_TOKEN=$token"); fi
  if [ "${1:-}" != pr ] || [ "${2:-}" != create ]; then
    exec env ${with[@]+"${with[@]}"} "$REAL_GH" "$@"
  fi

  # `gh pr create`: made as asked, then the agency is told which pull request.
  env ${with[@]+"${with[@]}"} "$REAL_GH" "$@"
  local status=$?
  if [ "$status" -eq 0 ]; then
    local head="" repo="" dry=0 previous="" arg url
    for arg in "$@"; do
      case "$previous" in -H | --head) head=$arg ;; -R | --repo) repo=$arg ;; esac
      case "$arg" in
        --head=*) head=${arg#--head=} ;;
        --repo=*) repo=${arg#--repo=} ;;
        --dry-run | --web | -w) dry=1 ;;
      esac
      previous=$arg
    done
    if [ "$dry" -eq 0 ]; then
      local -a view=(pr view)
      [ -n "$head" ] && view+=("$head")
      [ -n "$repo" ] && view+=(--repo "$repo")
      url=$(env ${with[@]+"${with[@]}"} "$REAL_GH" "${view[@]}" --json url --jq .url 2>/dev/null || true)
      if [ -z "$url" ] || ! flotta-pr report "$url" >/dev/null 2>&1; then
        echo "flotta: the agency was not told about this pull request. Run: flotta-pr report <its url>" >&2
      fi
    fi
  fi
  exit "$status"
}

# Something already authenticated this shell. Its choice wins.
if [ -n "${GH_TOKEN:-}" ] || [ -n "${GITHUB_TOKEN:-}" ]; then
  run_gh "" "$@"
fi

# A control-plane round trip is only worth it inside a repository that lives on
# GitHub. Outside one — `gh --help`, `gh api` in /tmp — pass straight through.
remote=$(git config --get remote.origin.url 2>/dev/null || true)
case "$remote" in
  *github.com[:/]*) ;;
  *) run_gh "" "$@" ;;
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
run_gh "$token" "$@"
