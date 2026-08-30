# The box image — a machine that *is* an agent, and can do work.
#
# ## Why Ubuntu and not python:slim (§6)
#
# The image this replaces was `python:3.11-slim` plus `git`. It could run
# Hermes and nothing else: no compiler, no node, no ripgrep, no `gh`. An agent
# on it could talk about a repository but not clone one, and could not run a
# test suite at all.
#
# §6 says to build this like exe.dev's `exeuntu` and steal the shape: a real
# machine, deliberately *un*-minimized, with the tools an engineer would
# expect. The reasoning that matters is theirs — "an agent that can read `man`
# is a better agent" — and it generalises: every tool missing from the image is
# a task the agent has to talk its way around instead of doing.
#
# What is NOT here yet, deliberately (§6b): systemd as PID 1, `unminimize` and
# man pages, headless Chrome, docker. Changing PID 1 is the risky half — the
# current entrypoint is what made durable memory work in M2 — so the toolchain
# lands first, where a failure is a missing binary rather than a box that will
# not boot.
FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive
ENV LANG=C.UTF-8

# The toolchain. Grouped by what it is for, because the next person's question
# is "can I add X" and the answer depends on which group it joins.
RUN apt-get update && apt-get install -y --no-install-recommends \
    # the basics an interactive session assumes exists
    ca-certificates curl wget gnupg less procps file tini \
    # source control — `gh` arrives separately, it is not in Ubuntu's archive
    git openssh-client \
    # building things: a compiler is the difference between installing a pure
    # Python package and installing one with a C extension
    build-essential pkg-config \
    # the search tools an agent actually reaches for. `rg` and `fd` are not
    # luxuries here: grepping a large repo with `grep -r` is slow enough that a
    # model will give up and guess instead.
    ripgrep fd-find jq \
    # languages. Python is Hermes's own; node is table stakes for most repos.
    python3 python3-venv python3-dev \
    # sqlite3 for inspecting Hermes's own state.db, which is a real debugging
    # need on a box you cannot easily copy files off
    sqlite3 \
    && rm -rf /var/lib/apt/lists/*

# Ubuntu ships `fd` as `fdfind` to avoid a name clash. Every piece of muscle
# memory and every model's training data says `fd`, so make that work.
RUN ln -s "$(command -v fdfind)" /usr/local/bin/fd

# `gh` is how an agent opens a pull request, which is the natural end of a task
# it was given. From GitHub's archive because Ubuntu's is far behind.
RUN mkdir -p -m 755 /etc/apt/keyrings \
    && curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
       -o /etc/apt/keyrings/githubcli-archive-keyring.gpg \
    && chmod go+r /etc/apt/keyrings/githubcli-archive-keyring.gpg \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
       > /etc/apt/sources.list.d/github-cli.list \
    && apt-get update && apt-get install -y --no-install-recommends gh \
    && rm -rf /var/lib/apt/lists/*

# node via nodesource: Ubuntu 24.04 ships Node 18, and enough of the ecosystem
# now assumes 20+ that the older one is a papercut on every `npm install`.
ARG NODE_MAJOR=22
RUN curl -fsSL https://deb.nodesource.com/setup_${NODE_MAJOR}.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*

# uv, for Python work that is not Hermes's own environment.
RUN curl -fsSL https://astral.sh/uv/install.sh | UV_INSTALL_DIR=/usr/local/bin sh

# ## Hermes lives in its own venv
#
# Ubuntu 24.04 enforces PEP 668, so `pip install` into the system Python fails
# with "externally-managed-environment". The usual workaround is
# `--break-system-packages`, and the name is accurate: it puts Hermes's pins in
# the same place as apt's, where an agent running `apt install python3-foo`
# later can stand on them.
#
# A venv costs one PATH entry and keeps the agent's own Python work — which is
# the entire point of this image — from being able to break the agent itself.
ENV HERMES_VENV=/opt/hermes-venv
ENV PATH="${HERMES_VENV}/bin:${PATH}"
RUN python3 -m venv "$HERMES_VENV"

ARG HERMES_REF
ARG HERMES_REPO=https://github.com/NousResearch/Hermes-Agent
RUN test -n "$HERMES_REF" || (echo "HERMES_REF build-arg is required" >&2; exit 1)

# Cloned rather than pip-installed from a URL: upstream blocks wheel and sdist
# builds, so `pip install 'hermes-agent @ git+...'` fails for anything after
# v2026.7.20. Both commands embed HERMES_REF so the layer key changes when the
# pin does.
ENV HERMES_SRC=/opt/hermes
RUN git clone --depth 1 --branch "$HERMES_REF" "$HERMES_REPO" "$HERMES_SRC" \
    && "$HERMES_VENV/bin/pip" install --no-cache-dir -e "$HERMES_SRC"

ENV HERMES_HOME=/data/hermes

# ## Where work happens — and where it must not
#
# `/data` is the volume: 1GB, and it holds the agent's memory. A repo clone or
# a `node_modules` there would fill it and take the memory with it.
#
# `/workspace` is on the rootfs (~7GB free), which survives a stop/start of the
# same machine and is lost on redeploy. That is the correct lifetime for
# working state: a clone is worth keeping across a nap and worth losing when
# the machine is replaced.
ENV FLOTTA_WORKDIR=/workspace
RUN mkdir -p /workspace
WORKDIR /workspace

# Flotta's own package, for `flotta.box.run` and the doctor.
COPY pyproject.toml README.md /app/
COPY src/ /app/src/
RUN "$HERMES_VENV/bin/pip" install --no-cache-dir -e /app

COPY fly/box_entrypoint.sh /usr/local/bin/box_entrypoint.sh
RUN chmod +x /usr/local/bin/box_entrypoint.sh

# tini as PID 1 so a process the agent spawns and abandons is reaped rather
# than accumulating as a zombie. An agent that runs `npm test` in a loop for a
# month will produce plenty of those, and nothing else in this container is
# going to clean them up.
ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/box_entrypoint.sh"]
