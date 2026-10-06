# The Slack bot as a container: bridge.py + headless Claude Code + the gotcha-support skill.
#
# Build and run with docker-compose.yml next to this file (context is the repo root, because
# the image needs both slack-bridge/ and support-agent-skill/). The container borrows the
# host's Tailscale daemon and ~/.ssh to reach the sites; see the compose file.
FROM python:3.12-slim-bookworm
# `make update` prunes the images it replaces by this label.
LABEL org.opencontainers.image.title=gotcha-support-bot

# Same uid/gid as the host user whose ~/.ssh, gotcha30 clone and tailscale access the bot
# uses: ssh refuses keys owned by someone else, and git refuses a repo owned by someone else.
ARG BOT_UID=1000
ARG BOT_GID=1000
# Pinned so a rebuild doesn't change the bot's behaviour under it. Bump on purpose.
ARG CLAUDE_VERSION=2.1.291

RUN apt-get update \
 && apt-get install -y --no-install-recommends bash ca-certificates coreutils curl git openssh-client procps \
 && rm -rf /var/lib/apt/lists/*

COPY slack-bridge/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

RUN groupadd -g "$BOT_GID" gotcha && useradd -m -u "$BOT_UID" -g "$BOT_GID" -s /bin/bash gotcha

# Claude Code goes in before the code is copied, so a code or KB change (`make update`) rebuilds
# only from the COPY lines down and doesn't download it again. It changes only when
# CLAUDE_VERSION does.
USER gotcha
ENV HOME=/home/gotcha
RUN curl -fsSL https://claude.ai/install.sh | bash -s "$CLAUDE_VERSION" \
 && mkdir -p /home/gotcha/.claude /home/gotcha/.cache/gotcha-support /home/gotcha/.local/state/gotcha-support

ENV PATH=/home/gotcha/.local/bin:$PATH \
    CLAUDE_BIN=/home/gotcha/.local/bin/claude \
    CLAUDE_CONFIG_DIR=/home/gotcha/.claude \
    DISABLE_AUTOUPDATER=1 \
    GOTCHA_REPO=/home/gotcha/.cache/gotcha-support/gotcha30 \
    PYTHONUNBUFFERED=1

# The code is root-owned: the bot can read its guard and skill, not change them.
USER root
COPY slack-bridge/ /app/slack-bridge/
COPY support-agent-skill/ /app/support-agent-skill/
RUN mkdir -p /app/slack-bridge/state && chown gotcha:gotcha /app/slack-bridge/state

USER gotcha
WORKDIR /app
CMD ["python3", "slack-bridge/bridge.py"]
