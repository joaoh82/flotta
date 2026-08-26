# The Flotta box image — a machine that IS an agent (M2).
#
# Deliberately NOT the `exeuntu` treatment yet (pivot doc §6: Ubuntu base,
# unminimize, systemd as PID 1, a full toolchain). That is the right end state
# and it is polish: none of it matters until something actually persists, and
# every layer added now is a layer to re-debug when it does not. This image
# exists to answer one question — does `/data/hermes` survive a stop/start —
# and it is kept small enough that the answer is unambiguous.
#
# It mirrors `src/flotta/box/image.py` on purpose, including the editable
# install: upstream Hermes *blocks* wheel and sdist builds, so
# `pip install 'hermes-agent @ git+...'` fails for anything after v2026.7.20.
# Their own setup.py names the way through — `build_editable` does not call
# `bdist_wheel`, so a source checkout installed with `-e` is the layout
# upstream intends, not a workaround.

FROM python:3.11-slim

# git: clone Hermes. ca-certificates: HTTPS to the provider.
# procps: `ps` inside `fly ssh console`, which is how you check whether the
# agent is actually running when it looks stuck.
RUN apt-get update \
    && apt-get install -y --no-install-recommends git ca-certificates procps \
    && rm -rf /var/lib/apt/lists/*

# Pinned, never floating. A moving ref means the image silently changes under
# you between builds and "which Hermes ran this?" stops being answerable.
# Passed from HERMES_REF in src/flotta/box/image.py so the box and the
# shard tier cannot drift apart.
ARG HERMES_REF
ARG HERMES_REPO=https://github.com/NousResearch/Hermes-Agent
RUN test -n "$HERMES_REF" || (echo "HERMES_REF build-arg is required" >&2; exit 1)

# `/opt/hermes`, and a plain `-e .` rather than the worker image's
# `/opt/hermes-agent` and `[mcp]` extra. The extra exists so a Tier 3 worker can
# serve an MCP endpoint; a box has nothing to serve yet (M3 turns the messaging
# gateway on and that changes). Noted so the difference reads as a decision
# rather than drift the next time someone diffs the two images.
ENV HERMES_SRC=/opt/hermes
RUN git clone --depth 1 --branch "$HERMES_REF" "$HERMES_REPO" "$HERMES_SRC" \
    && pip install --no-cache-dir -e "$HERMES_SRC"

# THE line this milestone exists for.
#
# v0.1 used /tmp/hermes. Everything that makes an agent worth hosting —
# conversation history, markdown memories, learned skills, session
# transcripts — lives under HERMES_HOME, and /tmp dies with the machine. The
# volume is mounted at /data by fly.toml; pointing HERMES_HOME inside it is
# what turns a container into a box.
ENV HERMES_HOME=/data/hermes

# The box's own code. Copied rather than installed from PyPI because there is
# no published package yet, and pinning a git ref of ourselves would be silly.
WORKDIR /app
COPY pyproject.toml README.md ./
COPY src/ ./src/
RUN pip install --no-cache-dir -e .

COPY fly/box_entrypoint.sh /usr/local/bin/box_entrypoint.sh
RUN chmod +x /usr/local/bin/box_entrypoint.sh

# The volume is mounted *at container start*, not at build time, so HERMES_HOME
# cannot be created in a layer — it would be masked by the mount. The
# entrypoint creates it on every boot instead.
ENTRYPOINT ["/usr/local/bin/box_entrypoint.sh"]
