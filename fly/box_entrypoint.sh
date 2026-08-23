#!/usr/bin/env bash
# PID 1 on a Flotta box.
#
# The box has no service to run yet — M3 turns Hermes's messaging gateway back
# on and makes this a real supervised process. Until then the box's job is to
# exist, keep its disk, and answer `fly ssh console`, so this sleeps forever
# and the proof script drives it over ssh.
#
# Sleeping rather than exiting is load-bearing: a machine whose process exits
# is restarted or marked failed by Fly, and neither is the "stopped, disk
# retained, costs pennies" state the whole pivot is built on.
set -euo pipefail

: "${HERMES_HOME:=/data/hermes}"

# Created on every boot, not in an image layer: the volume mounts over /data at
# container start, so anything the build wrote there is invisible afterwards.
mkdir -p "$HERMES_HOME"

echo "[flotta-box] HERMES_HOME=$HERMES_HOME"
echo "[flotta-box] mount:"
df -h /data 2>/dev/null || echo "  (no /data mount — the volume is missing)"

# `exec` so signals reach PID 1 directly and `fly machine stop` is a clean
# SIGTERM rather than a timeout.
exec sleep infinity
