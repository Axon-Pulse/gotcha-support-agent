# The Slack bot as a Docker service on this machine.
# Run from slack-bridge/, or from the repo root as `make -C slack-bridge <target>`.
SHELL := /bin/bash
.SHELLFLAGS := -eu -o pipefail -c
.ONESHELL:
.DEFAULT_GOAL := help

COMPOSE := docker compose
IMAGE   := gotcha-support-bot
# The foreground bot (`make dev`, or the venv python by hand). The [.] keeps pgrep from
# matching this recipe's own shell, whose command line contains the pattern.
FOREGROUND := [.]venv/bin/python.*bridge[.]py

.PHONY: help check build run update stop restart logs status shell test dev

help:  ## List the targets
	@grep -E '^[a-z]+:.*## ' $(MAKEFILE_LIST) | awk -F':.*## ' '{printf "  make %-8s %s\n", $$1, $$2}'

check:  ## Check .env and what the container borrows from this host
	@fail() { echo "check: $$*" >&2; exit 1; }
	[ -f .env ] || fail "no .env. Run: cp .env.example .env && chmod 600 .env, then fill it in"
	[ "$$(stat -c %a .env)" = 600 ] || echo "check: warning: .env is readable by others; run: chmod 600 .env" >&2
	for k in SLACK_BOT_TOKEN=xoxb- SLACK_APP_TOKEN=xapp- SLACK_ALLOWED_CHANNELS=C ANTHROPIC_API_KEY=sk-ant-; do
	  grep -qE "^$${k}.+" .env || fail "$${k%%=*} is missing or still the placeholder in .env"
	done
	uid=$$(sed -n 's/^BOT_UID=//p' .env); uid=$${uid:-1000}
	[ "$$(id -u)" = "$$uid" ] || fail "you are uid $$(id -u) but the image uses $$uid: set BOT_UID/BOT_GID in .env"
	[ -S /var/run/tailscale/tailscaled.sock ] || fail "tailscaled is not running on this host"
	[ -d "$$HOME/.ssh" ] || fail "no $$HOME/.ssh: the bot reaches the sites with this user's keys"
	git -C "$$HOME/.cache/gotcha-support/gotcha30" rev-parse --git-dir >/dev/null 2>&1 \
	  || fail "no gotcha30 clone. Run: ../support-agent-skill/scripts/code.sh clone"
	mkdir -p "$$HOME/.local/state/gotcha-support"
	echo "check: ok"

build: check  ## Build the image from the current files
	$(COMPOSE) build

run: check  ## Start the service (stays up across reboots and crashes)
	@if pgrep -f '$(FOREGROUND)' >/dev/null; then
	  echo "A foreground bridge.py is running (pid $$(pgrep -f '$(FOREGROUND)' | xargs)). Stop it first:" \
	       "two bots on one Slack token split the questions between them." >&2; exit 1
	fi
	$(COMPOSE) up -d
	echo "Started. Watch it with: make logs"

update: check  ## Rebuild from the current files and restart, after a code or KB change
	@if pgrep -f '$(FOREGROUND)' >/dev/null; then
	  echo "A foreground bridge.py is running. Stop it first." >&2; exit 1
	fi
	$(COMPOSE) up -d --build
	# The image each rebuild replaces, and only this bot's.
	docker image prune -f --filter label=org.opencontainers.image.title=$(IMAGE) >/dev/null
	echo "Updated. Watch it with: make logs"

stop:  ## Stop the service (it stays stopped after a reboot)
	$(COMPOSE) down

restart:  ## Restart without rebuilding, e.g. after editing .env
	$(COMPOSE) up -d --force-recreate

logs:  ## Follow the log
	$(COMPOSE) logs -f --tail 100

status:  ## Is it running, and since when
	$(COMPOSE) ps

shell:  ## A shell in the running container, for checks like `tailscale ping`
	$(COMPOSE) exec bot bash

test:  ## Unit tests (guard and bridge), with the local venv
	.venv/bin/python -m pytest tests -q

dev:  ## Run in the foreground with .env, without Docker (Ctrl-C stops)
	@[ -f .env ] || { echo "no .env; see make check" >&2; exit 1; }
	if [ -n "$$($(COMPOSE) ps -q --status running 2>/dev/null)" ]; then
	  echo "The Docker service is running. Run: make stop" >&2; exit 1
	fi
	set -a; . ./.env; set +a
	exec .venv/bin/python bridge.py
