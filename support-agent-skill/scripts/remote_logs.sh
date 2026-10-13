#!/usr/bin/env bash
# Read one container's log on a gotcha machine: `docker logs --tail N`, nothing else.
#
#   remote_logs.sh <tailnet-host> <container> [lines|all] [--grep REGEX]
#
# <container> is an exact name or a fragment (`dumbo` finds `gotcha-dumbo-1`); a fragment that
# matches nothing, or several, lists the containers on the machine instead of guessing.
# [lines] defaults to 100; `all` is the whole log. --grep keeps only matching lines, searching
# the whole log (case-insensitive extended regex), then shows the last [lines] of them (`all`:
# every match). No cap beyond the time limit: the caller cuts the output down with a pipe. Lines are
# shown whole: a stack trace or a JSON record cut short can hide the one field that matters.
# The output goes through redact.sed like the triage's. Never follows (-f), never writes.
# Settings as for run_triage.sh: GOTCHA_SSH_USER, GOTCHA_SSH, GOTCHA_TRIAGE_TIMEOUT (default 30).
set -uo pipefail

usage() { echo "usage: $0 <tailnet-host> <container> [lines|all] [--grep REGEX]" >&2; exit 2; }
[ $# -ge 2 ] || usage
HOST=$1; NAME=$2; shift 2
N=100; PAT=""
if [ $# -gt 0 ] && [[ $1 =~ ^([0-9]+|all)$ ]]; then N=$1; shift; fi
if [ "${1:-}" = "--grep" ]; then [ $# -eq 2 ] || usage; PAT=$2; shift 2; fi
[ $# -eq 0 ] || usage
[[ $NAME =~ ^[A-Za-z0-9][A-Za-z0-9_.-]{0,100}$ ]] || { echo "refusing container name '$NAME' (letters, digits, _ . - only)" >&2; exit 2; }
[[ $HOST =~ ^[A-Za-z0-9][A-Za-z0-9_.-]{0,100}$ ]] || { echo "refusing host '$HOST'" >&2; exit 2; }
[ "$N" = all ] || { [ "${#N}" -le 9 ] && [ "$N" -ge 1 ]; } || { echo "lines must be a number from 1, or all" >&2; exit 2; }
[ ${#PAT} -le 1000 ] || { echo "--grep pattern too long" >&2; exit 2; }

HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
TARGET="${GOTCHA_SSH_USER:+$GOTCHA_SSH_USER@}$HOST"
LIMIT=${GOTCHA_TRIAGE_TIMEOUT:-30}
READ=$(( LIMIT > 15 ? LIMIT - 5 : 10 ))     # docker logs stops before the ssh call is cut off
command -v tailscale >/dev/null || { echo "tailscale is not installed on this machine" >&2; exit 3; }
if ! pong=$(timeout 8 tailscale ping -c 1 --timeout 5s "$HOST" 2>&1); then
  echo "UNREACHABLE: no reply from $HOST ($(head -1 <<<"$pong"))"; exit 10
fi
. "$HERE/lib_ssh.sh"
pick_transport "$HOST" "$TARGET"

{
  printf 'NAME=%q\nN=%q\nPAT=%q\nREAD=%q\n' "$NAME" "$N" "$PAT" "$READ"
  cat <<'REMOTE'
export LC_ALL=C
if docker info >/dev/null 2>&1; then D=(docker)
elif sudo -n docker info >/dev/null 2>&1; then D=(sudo -n docker)
else echo "no docker access for $(id -un) (not in docker group, no passwordless sudo)"; exit 3; fi
names=$("${D[@]}" ps -a --format '{{.Names}}' 2>/dev/null)
c=$(printf '%s\n' "$names" | grep -Fx -- "$NAME")
[ -z "$c" ] && c=$(printf '%s\n' "$names" | grep -iF -- "$NAME")
if [ "$(printf '%s\n' "$c" | grep -c .)" -ne 1 ]; then
  echo "no single container matches '$NAME'. Containers on this machine (docker ps -a):"
  "${D[@]}" ps -a --format '{{.Names}}\t{{.Status}}'
  exit 4
fi
echo "container: $c   $("${D[@]}" inspect -f 'state={{.State.Status}} started={{.State.StartedAt}} exit={{.State.ExitCode}} restarts={{.RestartCount}}' "$c" 2>/dev/null)"
if [ -n "$PAT" ]; then
  if [ "$N" = all ]; then
    echo "(lines matching /$PAT/ in the whole log; all shown)"
    timeout "$READ" "${D[@]}" logs -t "$c" 2>&1 | grep -a -E -i -- "$PAT"
  else
    echo "(lines matching /$PAT/ in the whole log; last $N shown)"
    timeout "$READ" "${D[@]}" logs -t "$c" 2>&1 | grep -a -E -i -- "$PAT" | tail -n "$N"
  fi
else
  timeout "$READ" "${D[@]}" logs -t --tail "$N" "$c" 2>&1
fi
REMOTE
} | timeout "$LIMIT" "${SSH[@]}" 'bash -s' 2>&1 | sed -E -f "$HERE/redact.sed"
rc=${PIPESTATUS[1]}
[ "$rc" -eq 124 ] && echo "TIMED OUT after ${LIMIT}s talking to $TARGET via $LABEL"
exit 0
