# The front door (M5b) — public TLS entrypoint to every box in the fleet.
#
# Deliberately *not* the box image. A box is a fat, un-minimised machine with a
# toolchain because an agent needs one; the door is a proxy with a token
# validator and should be as small as the thing it does. It is also the one
# component that must never sleep, so it is the one whose size is a running
# cost rather than a build-time inconvenience.
FROM python:3.12-slim

# uv, for the same lockfile the rest of the project uses. Installing from the
# lock rather than resolving here means the door runs the versions that were
# tested, not whatever PyPI served that morning.
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app

# Dependencies before source: the layer only rebuilds when the lock changes,
# which is rarely, so a source edit is a fast rebuild.
COPY pyproject.toml uv.lock README.md ./
COPY src/ ./src/
RUN uv sync --frozen --extra door --no-dev

ENV PATH="/app/.venv/bin:$PATH"

# 0.0.0.0, NOT `::` — and this is the opposite of the box image on purpose.
#
# Fly Proxy requires a routable service to listen on 0.0.0.0. A box binds `::`
# because it is reached *from other machines* over 6PN, which is IPv6-only; the
# door is reached *by the proxy*. Binding `::` here bound v6-only, so every
# health check reported "connect: connection refused" while the door was
# serving fine over IPv6 — the failure looked like a crash and was a bind.
#
# The door's outbound calls to boxes are over 6PN regardless; a socket's bind
# address has nothing to do with what it can dial.
ENV FLOTTA_DOOR_HOST="0.0.0.0"
ENV FLOTTA_DOOR_PORT="8080"

# Reports the door's own liveness and nothing about the fleet, so it is safe to
# leave unauthenticated and safe for Fly to poll.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/_door/health', timeout=3).status==200 else 1)"

CMD ["sh", "-c", "exec flotta door --host \"$FLOTTA_DOOR_HOST\" --port \"$FLOTTA_DOOR_PORT\""]
