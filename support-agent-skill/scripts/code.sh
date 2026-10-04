#!/usr/bin/env bash
# Look things up in the gotcha30 source at the exact version a system runs. Everything except
# `sync` and `clone` works offline, from the local clone.
#
#   code.sh sync                                  fetch new commits/tags (quietly skipped when offline)
#   code.sh resolve <ver>                         print the commit a version means
#   code.sh grep    <ver> <regex> [path...]       git grep at that commit (case-insensitive, line numbers)
#   code.sh show    <ver> <file> [start[,end]]    a file at that commit, optionally a line range
#   code.sh log     <ver> [path...]               last 20 commits up to that version (touching path)
#   code.sh clone                                 first-time clone into ~/.cache/gotcha-support/gotcha30
#
# <ver> can be:
#   a system name        axon-gotcha-5   -> its version from the last successful triage (systems.tsv in the state dir, see below)
#   a launcher version   v1.3.0-38-g7c9b0167 (from `system_launcher --version`) -> commit 7c9b0167
#   a release tag        v1.3.0
#   an image tag         stable -> newest v* tag, latest -> origin/main, sha-7c9b016 -> that commit
#   a commit             7c9b0167
# Anything unresolvable (e.g. a system never triaged) falls back to the newest release tag,
# with a warning on stderr saying so.
#
# State dir: $GOTCHA_STATE_DIR, else ${XDG_STATE_HOME:-~/.local/state}/gotcha-support.
# Repo: $GOTCHA_REPO, else ~/gotcha30 if it is a gotcha30 clone, else ~/.cache/gotcha-support/gotcha30.
# Nothing here touches a working tree: it reads objects by commit, so a clone someone is
# developing in is safe to use.
set -uo pipefail
HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
# Where the per-system memory (host, release, config, gotcha dir) lives. Outside the skill
# folder, which can be read-only or replaced on update. Shared with run_triage.sh.
STATE_DIR=${GOTCHA_STATE_DIR:-${XDG_STATE_HOME:-$HOME/.local/state}/gotcha-support}
STATE="$STATE_DIR/systems.tsv"
OLD_STATE="$HERE/../state/systems.tsv"   # where earlier versions kept it; copied over once
if [ ! -f "$STATE" ] && [ -f "$OLD_STATE" ]; then mkdir -p "$STATE_DIR" 2>/dev/null && cp "$OLD_STATE" "$STATE" 2>/dev/null; fi
URL=${GOTCHA_REPO_URL:-git@github.com:Axon-Pulse/gotcha30.git}
CACHE="$HOME/.cache/gotcha-support/gotcha30"

pick_repo() {
  local r
  for r in "${GOTCHA_REPO:-}" "$HOME/gotcha30" "$CACHE"; do
    [ -n "$r" ] && git -C "$r" rev-parse --git-dir >/dev/null 2>&1 \
      && git -C "$r" remote get-url origin 2>/dev/null | grep -qi 'gotcha30' && { echo "$r"; return; }
  done
  return 1
}

cmd=${1:-}; shift || true
if [ "$cmd" = clone ]; then
  mkdir -p "$(dirname "$CACHE")"
  exec git clone --quiet "$URL" "$CACHE"
fi
# The range of `show` goes into a sed script and an arithmetic expansion, and sed's `e` command
# runs a shell command: anything but line numbers would be code execution. Checked before
# anything else happens, so it holds even where there is no clone to read.
if [ "$cmd" = show ] && [ -n "${3:-}" ] && ! [[ ${3} =~ ^[0-9]+(,[0-9]+)?$ ]]; then
  echo "range must be N or N,M (line numbers), got '${3}'" >&2; exit 2
fi
REPO=$(pick_repo) || { echo "no gotcha30 clone found. Run: $0 clone   (or set GOTCHA_REPO)" >&2; exit 3; }
G() { git -C "$REPO" "$@"; }

resolve() {
  local v=$1 sha=""
  # a system name: use the version its last triage recorded
  if [ -f "$STATE" ]; then
    local rec; rec=$(awk -F'\t' -v h="$v" 'tolower($1)==tolower(h){r=$0} END{print r}' "$STATE")
    if [ -n "$rec" ]; then
      echo "$v: last triaged $(cut -f2 <<<"$rec"), release $(cut -f3 <<<"$rec"), image tag $(cut -f4 <<<"$rec")" >&2
      v=$(cut -f3 <<<"$rec"); [ "$v" = unknown ] && v=$(cut -f4 <<<"$rec")
    fi
  fi
  v=${v%-dirty}
  case $v in
    *-g[0-9a-f]*) sha=${v##*-g} ;;
    sha-*)        sha=${v#sha-} ;;
    stable)       sha=$(G tag --list 'v*' --sort=-v:refname | head -1) ;;
    latest|main)  sha=origin/main ;;
  esac
  [ -z "$sha" ] && sha=$v
  if ! G rev-parse --verify -q "$sha^{commit}" >/dev/null; then
    # Unknown system or version: assume the newest release, which is what deployed machines
    # track (:stable). Not origin/main, which may hold behaviour no site runs yet.
    sha=$(G tag --list 'v*' --sort=-v:refname | head -1)
    echo "WARNING: no recorded or known version for '$1'; assuming newest release $sha — say so in the answer (if a newer release exists, run: $0 sync)" >&2
  fi
  G rev-parse --short=10 "$sha^{commit}"
  echo "resolved '$1' -> $(G describe --tags --match 'v*' --always "$sha" 2>/dev/null) ($(G log -1 --format=%cs "$sha"))" >&2
}

case $cmd in
  sync)
    timeout 30 git -C "$REPO" fetch --quiet --tags origin 2>/dev/null \
      && echo "fetched; origin/main is $(G log -1 --format='%h %cs' origin/main)" \
      || echo "offline or fetch failed; using what the clone already has (origin/main $(G log -1 --format='%h %cs' origin/main))" ;;
  resolve) resolve "${1:?version}" ;;
  grep)
    c=$(resolve "${1:?version}") || exit 1; re=${2:?regex}; shift 2
    G grep -n -p -I -i -E -e "$re" "$c" -- "${@:-.}" | sed "s/^$c://" | cut -c1-240 | head -80 ;;
  show)
    c=$(resolve "${1:?version}") || exit 1; f=${2:?file}; range=${3:-}
    if [ -n "$range" ]; then
      s=${range%,*}; e=${range#*,}; [ "$e" = "$range" ] && e=$((s + 60))
      G show "$c:$f" | sed -n "${s},${e}{=;p}" | sed 'N;s/\n/\t/'
    else
      G show "$c:$f" | head -400
    fi ;;
  log)
    c=$(resolve "${1:?version}") || exit 1; shift
    G log --format='%h %cs %s' -20 "$c" -- "$@" ;;
  *) sed -n '2,20p' "$0"; exit 2 ;;
esac
