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

echo "[flotta-box] HERMES_HOME=$HERMES_HOME"
echo "[flotta-box] mount:"
df -h /data 2>/dev/null || echo "  (no /data mount — the volume is missing)"

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
