#!/usr/bin/env bash
# PID 1 on a Flotta box — Hermes, serving.
#
# M2 ran `sleep infinity` here: the box existed and kept a disk, but nothing
# was listening and every turn was a fresh `python -m flotta.box.run` over ssh.
# M3 makes the box an agent you can *talk to*, which is the difference between
# a machine that stores memory and one that has it.
#
# ## Why `hermes serve` and not `hermes gateway`
#
# The pivot doc (§M3) says "turn the messaging gateway back on". There are two
# things with that name and only one is what we want:
#
#   hermes gateway  — adapters for Telegram / Discord / WhatsApp / Weixin.
#                     Real, and a fine future feature, but it is "text your
#                     agent", not a surface a client can drive.
#   hermes serve    — the same gateway headless: JSON-RPC/WS on 127.0.0.1:9119,
#                     "what the desktop app and remote backends run".
#
# `flotta chat` needs the second. The first can be layered on later without
# changing anything here.
#
# ## Why it binds 0.0.0.0, and why that is not a hole
#
# The first design here bound loopback and tunnelled in with `flyctl proxy`.
# It does not work: `flyctl proxy` forwards to the machine's *WireGuard*
# address, so a process on the VM's 127.0.0.1 is unreachable through it. The
# tempting fix — a socat forwarder from the 6PN address to loopback — would
# bypass Hermes's auth gate rather than satisfy it, and that gate exists
# because of a real campaign (`hermes-0day`) against open dashboards.
#
# ## Why `::` and not `0.0.0.0`
#
# **Fly's private network is IPv6-only.** `0.0.0.0` binds IPv4 only, so a box
# serving on it is invisible to `flyctl proxy`, which dials the machine's
# `fdaa:` address — the connection is reset and nothing in the logs says why.
# Diagnosed by reading `/proc/net/tcp6` on the box: the sole IPv6 listener was
# port 22. `::` binds IPv6 and, with Linux dual-stack, accepts IPv4 too.
#
# ## Why binding all interfaces is not a hole
#
# The box binds all interfaces and *satisfies* the gate with the bundled
# basic-auth provider. Two independent layers protect it:
#
#   1. There is no public route. fly.toml declares no service and the app has
#      no public IP, so the only way in is Fly's private WireGuard network.
#   2. Hermes's own auth gate, which refuses to serve a non-loopback bind
#      without a provider configured.
#
# Neither is load-bearing alone, which is the point. §M5 adds the public front
# door and scoped tokens deliberately, rather than inheriting one by accident.
set -euo pipefail

: "${HERMES_HOME:=/data/hermes}"
: "${FLOTTA_SERVE_PORT:=9119}"
: "${FLOTTA_SERVE_HOST:=::}"

# Created on every boot, not in an image layer: the volume mounts over /data at
# container start, so anything the build wrote there is invisible afterwards.
mkdir -p "$HERMES_HOME"

# The working directory (§6a). On the rootfs, not the volume, and the split is
# load-bearing: /data is ~1GB and holds the agent's memory, so one `npm
# install` there would fill it and take the memory with it. /workspace has the
# machine's whole rootfs (~7GB) and the right lifetime — a clone survives a
# nap, and is lost when the machine is replaced.
: "${FLOTTA_WORKDIR:=/workspace}"
mkdir -p "$FLOTTA_WORKDIR"

echo "[flotta-box] HERMES_HOME=$HERMES_HOME"
echo "[flotta-box] workdir=$FLOTTA_WORKDIR"
echo "[flotta-box] mount:"
df -h /data 2>/dev/null || echo "  (no /data mount — the volume is missing)"
df -h "$FLOTTA_WORKDIR" 2>/dev/null | tail -1

# Loud, because it is the number that decides whether a task can run at all and
# the failure without it is a build that dies halfway with ENOSPC.
free_kb=$(df -Pk "$FLOTTA_WORKDIR" 2>/dev/null | awk 'NR==2 {print $4}')
if [ -n "$free_kb" ] && [ "$free_kb" -lt 1048576 ]; then
  echo "[flotta-box] WARNING: less than 1GB free in $FLOTTA_WORKDIR — builds will fail"
fi

# ## Git identity (FLOTTA-20)
#
# Written on every boot from the environment rather than baked into the image
# or kept on the volume. A box's identity is a property of the fleet record, so
# renaming one or pointing it at a different control plane should take a
# restart, not a rebuild.
export HOME="${HOME:-/root}"
BOX_NAME="${FLOTTA_BOX_NAME:-${FLY_APP_NAME:-box}}"

# An agent asked to authenticate interactively hangs forever: there is no
# terminal and nobody is watching. Fail fast instead — git's own error names
# the repository, which is what a human needs to see.
export GIT_TERMINAL_PROMPT=0
export GH_PROMPT_DISABLED=1

git config --global --replace-all user.name "$BOX_NAME"

# ## The commit address must be one that cannot belong to a person
#
# This was `${BOX_NAME}@users.noreply.github.com`, on the belief that only
# `<id>+<login>@users.noreply.github.com` resolves to an account. **That is
# wrong.** The legacy noreply format is exactly `<login>@users.noreply.github.com`
# and GitHub still links it — a box named `eng-a` had its first commit
# attributed to github.com/Eng-A, a real account belonging to a stranger,
# confirmed live on the pushed commit. Any box whose name happens to match a
# GitHub username adopts that user's identity on everything it writes.
#
# So the address comes from a domain nobody else can hold. `$FLOTTA_DOMAIN`
# when the fleet has one; otherwise `.invalid`, which RFC 2606 reserves and no
# registry will ever sell (RFC 2606, carried forward by RFC 6761) — an address
# that cannot be verified on a GitHub
# account cannot be linked to one.
#
# Not "pick a name that is not taken": usernames are registered continuously,
# so that is a property that can stop being true after the box is created.
: "${FLOTTA_GIT_EMAIL_DOMAIN:=${FLOTTA_DOMAIN:-boxes.invalid}}"
git config --global --replace-all user.email "${BOX_NAME}@${FLOTTA_GIT_EMAIL_DOMAIN}"
git config --global --replace-all init.defaultBranch main

# The repository path is what makes per-box grants possible at all. Without it
# git asks for "a credential for github.com" and a grant has nothing to key on.
git config --global --replace-all credential.useHttpPath true

if [ -n "${FLOTTA_CONTROL_URL:-}" ] && [ -n "${FLOTTA_BOX_TOKEN:-}" ] \
   && [ -n "${FLOTTA_BOX_ID:-}" ]; then
  git config --global --replace-all credential.helper flotta
  echo "[flotta-box] git: $BOX_NAME <${BOX_NAME}@${FLOTTA_GIT_EMAIL_DOMAIN}>, credentials via control plane"
else
  # Unset rather than left over: a helper configured with nothing behind it
  # costs a failed round trip on every fetch and reports it as a credential
  # error, which reads like a revoked grant rather than a box nobody set up.
  git config --global --unset-all credential.helper || true
  echo "[flotta-box] git: $BOX_NAME, NO credential helper — public repositories only"
  echo "[flotta-box]   fix with: just box-identity $BOX_NAME"
fi

# Fail loudly rather than serve something unreachable. Hermes refuses a
# non-loopback bind with no auth provider, so without these the box would boot,
# exit, and be restarted forever with the reason buried in the logs.
if [ "$FLOTTA_SERVE_HOST" != "127.0.0.1" ]; then
  : "${HERMES_DASHBOARD_BASIC_AUTH_USERNAME:?set it with: just fly-auth}"
  : "${HERMES_DASHBOARD_BASIC_AUTH_PASSWORD:?set it with: just fly-auth}"
  : "${HERMES_DASHBOARD_BASIC_AUTH_SECRET:?set it with: just fly-auth}"
fi

# Serving is what creates Hermes's session schema. Under M2 the box's state.db
# held one table (`async_delegations`); `hermes serve` brings up all 22 —
# sessions, messages, the FTS indexes — so conversation history starts
# surviving a restart from here on, on the durable volume.
echo "[flotta-box] starting hermes serve on ${FLOTTA_SERVE_HOST}:${FLOTTA_SERVE_PORT}"

# `exec` so signals reach PID 1 directly and `fly machine stop`/`suspend` is a
# clean SIGTERM rather than a timeout. It also means the *agent* is the process
# Fly snapshots on suspend — which is the point of preferring suspend at all:
# M1 measured that suspend keeps the VM's memory, and until now there was
# nothing in that memory worth keeping.
exec hermes serve --no-open --host "$FLOTTA_SERVE_HOST" --port "$FLOTTA_SERVE_PORT"
