# The control plane — §8.1's "boring block".
#
# Deliberately unremarkable: ~256MB, a public HTTPS endpoint, no volumes, no
# machines API, nothing that needs a particular provider. That is the whole
# point of §8.2's split — the interesting requirements live below the `Backend`
# line, and keeping this block boring is what makes the Railway recipe (M5.5)
# possible at all.
#
# Not the box image. `fly/Dockerfile` builds a machine that *is* an agent, with
# Hermes and a durable volume. This builds a small web service that watches
# them.

FROM python:3.11-slim

# libpq for psycopg's binary wheel, and nothing else. Every apt package here is
# one the control plane genuinely cannot start without.
RUN apt-get update \
    && apt-get install -y --no-install-recommends libpq5 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src/ ./src/

# `[server]` = postgres + control. A deployed control plane always wants both:
# fleet state on a server is the whole reason this process exists.
RUN pip install --no-cache-dir ".[server]"

# Fleet state comes from the environment, never baked in. §8.3 is specific that
# the Railway template must wire DATABASE_URL *by reference* rather than
# copy-pasting it, because a hardcoded value breaks on first redeploy.
ENV FLOTTA_DATABASE_URL=""

# 0.0.0.0 because a container's loopback is reachable by nothing. The bind
# guard refuses this unless FLOTTA_CONTROL_ALLOW_INSECURE_BIND is set, which is
# the correct friction until M5 adds tokens: a platform that terminates TLS and
# authenticates in front of this (Railway, Fly) is the operator asserting they
# own the perimeter.
ENV FLOTTA_CONTROL_HOST=0.0.0.0
# Unset by default so $PORT can win; the CMD falls back to 8080 when neither
# is set. Setting it here would shadow the platform's own variable.
ENV FLOTTA_CONTROL_PORT=""
EXPOSE 8080

# `/health` returns 503 when the reconcile loop has stopped sweeping, so a
# platform health check catches a slept loop — §8.3's footgun — instead of it
# being discovered days later from a task stranded at `running`.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request,os,sys; \
url='http://127.0.0.1:'+(os.environ.get('FLOTTA_CONTROL_PORT') or os.environ.get('PORT') or '8080')+'/health'; \
sys.exit(0 if urllib.request.urlopen(url, timeout=4).status == 200 else 1)"

# `${PORT:-...}` because PaaS platforms inject the port they expect the app to
# listen on, and Railway — §8.3's self-host target — fails a deploy outright
# when nothing answers there. Honouring $PORT makes the image work unmodified
# on Railway, Render and Fly, while FLOTTA_CONTROL_PORT still wins for anyone
# who sets it deliberately.
CMD ["sh", "-c", "flotta serve --host \"$FLOTTA_CONTROL_HOST\" --port \"${FLOTTA_CONTROL_PORT:-${PORT:-8080}}\""]
