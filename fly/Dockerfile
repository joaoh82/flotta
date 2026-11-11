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
# man pages, docker. A browser is: a headed Chromium on the agent's screen
# (Screen.2, below) does everything headless Chrome would.
#
# Changing PID 1 is the risky half — the
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
#
# The venv is built from **Hermes's own `uv.lock` and `.python-version`**, not
# from Ubuntu's Python and pip. From v0.22.x every core dependency carries
# `; python_version >= '3.14'`, so `pip install -e` on Ubuntu's 3.12 succeeded,
# installed none of them, and every box boot-looped on `No module named
# 'ruamel'`; on 3.14 pip cannot resolve the pins at all. The lock is what
# upstream tests, and older tags (`.python-version` 3.11) take the same path.
ENV UV_PYTHON_INSTALL_DIR=/opt/uv-python

ARG HERMES_REF
ARG HERMES_REPO=https://github.com/NousResearch/Hermes-Agent
RUN test -n "$HERMES_REF" || (echo "HERMES_REF build-arg is required" >&2; exit 1)

# Cloned rather than pip-installed from a URL: upstream blocks wheel and sdist
# builds, so `pip install 'hermes-agent @ git+...'` fails for anything after
# v2026.7.20. Both commands embed HERMES_REF so the layer key changes when the
# pin does.
#
# The trailing import fails the build when a dependency silently did not
# install, instead of on every agent that takes the image.
ENV HERMES_SRC=/opt/hermes
RUN git clone --depth 1 --branch "$HERMES_REF" "$HERMES_REPO" "$HERMES_SRC" \
    && cd "$HERMES_SRC" \
    && UV_PROJECT_ENVIRONMENT="$HERMES_VENV" uv sync --frozen --no-dev --no-cache \
       --extra computer-use \
       --python-preference only-managed \
    && "$HERMES_VENV/bin/python" -c "import hermes_cli.main"

# ## The screen (Screen.2, FLOTTA-207; docs/AGENT_SCREEN.md in flotta_parent)
#
# Hermes ships a per-agent screen it calls Bot Desktop: an Xvnc + Xfce desktop
# the agent drives with `computer_use` and a headed Chromium, which a person can
# watch and take over. Everything it needs is baked in here, because a box
# cannot install it at run time in any way worth depending on:
#
# - **Not on the volume.** Hermes's PM keeps its tools in `$HERMES_HOME/tools`,
#   which is /data: Chromium, agent-browser and cua-driver are ~750 MB, and the
#   volume is 1 GB and holds the agent's memory. So PM installs them under a
#   home of its own on the rootfs, and the env below points Hermes at them.
# - **Not at run time.** A Fly restart resets the rootfs, so anything installed
#   by hand on a running box is gone at the next boot (found in Screen.1).
# - **Not snap.** Ubuntu 24.04's `chromium` is a snap stub, so the browser is
#   PM's pinned build, and its shared libraries are apt's (the second group).
#
# Nothing starts here, and `bot_desktop.auto_start` stays Hermes's default
# (false): an agent without a screen pays only disk for this layer, never
# memory. `flotta.box.screen` turns it on per agent (Screen.3).
#
# Deliberately not the `xfce4` metapackage: it pulls in a screensaver, a power
# manager and a polkit agent that lock or prompt a headless desktop.
RUN apt-get update && apt-get install -y --no-install-recommends \
    # the desktop Hermes's Bot Desktop launches: Xvnc, Xfce's pieces, and the
    # X utilities its launcher probes
    tigervnc-standalone-server xfce4-panel xfwm4 xfdesktop4 xfce4-settings \
    xfce4-terminal dbus-x11 x11-xserver-utils x11-utils x11-xkb-utils xauth \
    fonts-dejavu-core at-spi2-core \
    # what PM's Chromium links against (Playwright's list for Ubuntu 24.04)
    libnss3 libnspr4 libatk1.0-0t64 libatk-bridge2.0-0t64 libcups2t64 libdrm2 \
    libxkbcommon0 libxcomposite1 libxdamage1 libxfixes3 libxrandr2 libgbm1 \
    libpango-1.0-0 libcairo2 libasound2t64 libatspi2.0-0t64 libxshmfence1 \
    fonts-liberation \
    && rm -rf /var/lib/apt/lists/*

# PM's pinned agent-browser (with its Chromium) and cua-driver, in a PM home on
# the rootfs. Their directories carry versions (`chromium-1208`, ...), so stable
# links in /opt/screen are what the env names; `hermes pm env` says where each
# landed, which keeps the version out of this file. The checks fail the build
# rather than every agent that later turns a screen on.
ENV FLOTTA_SCREEN_PM_HOME=/opt/hermes-pm
RUN HERMES_HOME="$FLOTTA_SCREEN_PM_HOME" hermes pm install agent-browser cua-driver
COPY fly/screen_tools.py /tmp/screen_tools.py
RUN HERMES_HOME="$FLOTTA_SCREEN_PM_HOME" hermes pm env > /tmp/pm-env.json \
    ; cat /tmp/pm-env.json | "$HERMES_VENV/bin/python" /tmp/screen_tools.py /opt/screen \
    && rm /tmp/pm-env.json /tmp/screen_tools.py \
    && /opt/screen/chrome --headless=new --no-sandbox --disable-gpu --dump-dom about:blank >/dev/null
ENV AGENT_BROWSER_EXECUTABLE_PATH=/opt/screen/chrome
ENV HERMES_CUA_DRIVER_CMD=/opt/screen/bin/cua-driver
ENV PATH="/opt/screen/bin:${PATH}"
# Chromium's launch flags. Hermes sets `--no-sandbox,--disable-dev-shm-usage`
# itself when it runs as root (a box always does) *unless* this is set, so the
# two are repeated here. The rest is the volume: the browser profile (sign-ins)
# lives in $HERMES_HOME/bot-desktop/browser-profile on /data, which is right,
# but Chromium keeps its cache inside the profile by default and that alone
# can fill a 1 GB disk that holds the agent's memory. So the cache goes to the
# rootfs, capped at 256 MB (Hermes's own cap for its dock Browser). A cache is
# worth losing on a re-image; a sign-in is not.
ENV AGENT_BROWSER_ARGS=--no-sandbox,--disable-dev-shm-usage,--disk-cache-dir=/var/cache/flotta-chromium,--disk-cache-size=268435456

# Which Hermes this image carries, recorded **in the image**.
#
# It was recorded nowhere before this line. `HERMES_REF` is a build arg: it
# decides what gets cloned and then evaporates, so a running agent could not
# say what it was running and neither could anything looking at it. The app
# showed the deployment tag instead, which says which *build* this is and
# nothing about which Hermes is inside it.
#
# A label rather than an `ENV` because a label cannot drift. `ENV` is machine
# configuration — it is set when a machine is created, survives a
# `machine update --image`, and would therefore keep asserting the old version
# after precisely the operation that changed it. The label travels with the
# image, so it is right by construction: change the image, change the answer.
#
# Fly reports image labels in `machines list --json` under `image_ref.labels`,
# which is how `FlyBackend.inspect` reads it back without asking the box.
LABEL dev.flotta.hermes-ref="$HERMES_REF"

ENV HERMES_HOME=/data/hermes

# ## Where work happens — and where it must not
#
# `/data` is the volume: 1GB, and it holds the agent's memory. A repo clone or
# a `node_modules` there would fill it and take the memory with it.
#
# `/workspace` is on the rootfs (~7GB free), which survives a stop/start of the
# same machine and is lost on redeploy *and* on every image update (`machine
# update --image`, which is what "Update agents" does). That is the correct
# lifetime for working state: a clone is worth keeping across a nap and worth
# losing when the machine is replaced (FLOTTA-56 / D15). The path is
# overridable as `$FLOTTA_WORKDIR` from Fleet settings; it must not be `/data`.
ENV FLOTTA_WORKDIR=/workspace
RUN mkdir -p /workspace
WORKDIR /workspace

# Flotta's own package, for `flotta.box.run` and the doctor.
COPY pyproject.toml README.md /app/
COPY src/ /app/src/
RUN uv pip install --no-cache --python "$HERMES_VENV" -e /app \
    && "$HERMES_VENV/bin/python" -c "import yaml, flotta.box.inference, flotta.box.approvals"

COPY fly/box_entrypoint.sh /usr/local/bin/box_entrypoint.sh
RUN chmod +x /usr/local/bin/box_entrypoint.sh

# ## GitHub identity (FLOTTA-20)
#
# `git-credential-flotta` is a console script from the package installed just
# above — git resolves `credential.helper = flotta` by looking for exactly that
# name on PATH, so the entry point in pyproject.toml *is* the wiring.
#
# The `gh` shim shadows /usr/bin/gh because `gh` has no credential-helper hook
# and the alternative is a long-lived token in its hosts.yml. Read the header
# of gh_shim.sh before changing it; it is deliberately transparent.
#
# `flotta-repos` and the `flotta-github-access` skill are how an agent learns
# which repositories it may use (FLOTTA-61). Both are checked here because a
# skill that names a missing command, or a command no skill mentions, fails
# quietly: the agent just goes back to guessing with a shell.
COPY fly/gh_shim.sh /usr/local/bin/gh
RUN chmod +x /usr/local/bin/gh \
    && test -x /usr/bin/gh \
    && test -x "$HERMES_VENV/bin/git-credential-flotta" \
    && test -x "$HERMES_VENV/bin/flotta-repos" \
    && test -x "$HERMES_VENV/bin/flotta-ask" \
    && test -x "$HERMES_VENV/bin/flotta-pr" \
    && test -x "$HERMES_VENV/bin/flotta-request" \
    && test -f /app/src/flotta/box/hermes_skills/flotta/flotta-github-access/SKILL.md \
    && test -f /app/src/flotta/box/hermes_skills/flotta/flotta-colleagues/SKILL.md \
    && test -f /app/src/flotta/box/hermes_skills/flotta/flotta-requests/SKILL.md

# tini as PID 1 so a process the agent spawns and abandons is reaped rather
# than accumulating as a zombie. An agent that runs `npm test` in a loop for a
# month will produce plenty of those, and nothing else in this container is
# going to clean them up.
ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/box_entrypoint.sh"]
