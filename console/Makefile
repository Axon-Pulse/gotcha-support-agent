# The support bot's console. Run from console/, or from the repo root as `make -C console <target>`.
SHELL := /bin/bash
.SHELLFLAGS := -eu -o pipefail -c
.ONESHELL:
.DEFAULT_GOAL := help

# The console's own port, on loopback. Teammates use the tailnet address `make serve` sets up,
# http://<this host>:8765, which forwards here.
PORT ?= 8766
ROOT := $(abspath ..)
PY := $(CURDIR)/.venv/bin/python
UVICORN := $(PY) -m uvicorn console.server:app --host 127.0.0.1 --port $(PORT)

.PHONY: help install dev run serve test

help:  ## List the targets
	@grep -E '^[a-z]+:.*## ' $(MAKEFILE_LIST) | awk -F':.*## ' '{printf "  make %-8s %s\n", $$1, $$2}'

install:  ## Create .venv and install the requirements
	python3 -m venv .venv
	.venv/bin/pip install -q -r requirements.txt
	[ -f .env ] || { cp .env.example .env; chmod 600 .env; echo "Created console/.env: set CONSOLE_ALLOWED_USERS"; }

dev:  ## Run on this machine only, local requests allowed (http://127.0.0.1:8766)
	cd $(ROOT) && CONSOLE_ALLOW_LOCAL=1 exec $(UVICORN) --reload --reload-dir console

run:  ## Run as the service does: only tailnet users in CONSOLE_ALLOWED_USERS get in
	[ -f .env ] && { set -a; . ./.env; set +a; }
	cd $(ROOT) && exec $(UVICORN)

serve:  ## Put it on the tailnet at http://<this host>:8765 (once, as root; tailscale keeps it)
	tailscale serve --bg --http 8765 http://127.0.0.1:$(PORT)
	tailscale serve status

test:  ## The console's tests (node runs the page's script, if installed)
	cd $(ROOT) && $(PY) -m pytest console/tests -q
