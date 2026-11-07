# The model gateway on Fly — every model call an agent makes, metered.
#
# **Why Fly, when it ran on Render.** On Render every request crosses Render's
# Cloudflare edge, whose managed WAF reads request bodies — and a model call's
# body is the whole conversation, including every shell command the agent ran.
# On 6 Nov 2026 an agent ran `sleep 120; curl -s -o /dev/null …` to wait for a
# deploy, the WAF matched it as an injection, and every later turn of that
# conversation was answered by Cloudflare's "Blocked" page, because each turn
# resends the history. Nothing reached the gateway, so nothing was logged here.
# On Fly the agents reach the gateway over 6PN: no edge, no WAF, no proxy hop.
#
# Not the control plane's image: no flyctl, no box build context. The gateway
# only checks tokens, reads the store and forwards to the provider.
FROM python:3.11-slim

# libpq for psycopg's binary wheel: the gateway reads Postgres on every call.
RUN apt-get update \
    && apt-get install -y --no-install-recommends libpq5 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src/ ./src/
# `[server]` = postgres + control, which is where the gateway's FastAPI lives.
RUN pip install --no-cache-dir ".[server]"

# `::`, like a box and unlike the door: the gateway is reached *from other
# machines* over 6PN, which is IPv6-only, and never through Fly's proxy. A
# single uvicorn process binds `::` v6-only, which is all 6PN needs.
ENV FLOTTA_GATEWAY_HOST="::"
ENV FLOTTA_GATEWAY_PORT="8080"
EXPOSE 8080

CMD ["sh", "-c", "exec flotta gateway --host \"$FLOTTA_GATEWAY_HOST\" --port \"$FLOTTA_GATEWAY_PORT\""]
