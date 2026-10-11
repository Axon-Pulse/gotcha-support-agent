#!/usr/bin/env bash
# Fingerprint of what the bot's image runs: the skill (SKILL.md, KB, scripts) and the bridge
# (bridge.py, the guard, the workspace settings, the image recipe, and docker-compose.yml, whose
# mounts and settings only reach the bot when it is recreated). `make build`/`update` stamp it on
# the image as the label gotcha.skill-hash, and the console compares it with the files on disk to
# say whether an edit has been deployed. One script, so the two can't count differently.
set -euo pipefail
cd "$(dirname "$0")/.."
find support-agent-skill/SKILL.md support-agent-skill/kb support-agent-skill/scripts \
     slack-bridge/bridge.py slack-bridge/bash_guard.py slack-bridge/requirements.txt \
     slack-bridge/Dockerfile slack-bridge/docker-compose.yml slack-bridge/workspace \
     -type f ! -name '*.pyc' -print0 \
  | LC_ALL=C sort -z | xargs -0 sha256sum | sha256sum | cut -c1-16
