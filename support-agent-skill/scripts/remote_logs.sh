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
# The output goes through redact.sed like the triage's. Never follows (-f), never writes on the
# site. A whole-log read keeps a redacted copy on this machine for a few minutes (see CACHE).
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

# A whole-log read (`all`, or any --grep) fetches the full log once, redacted, and keeps it here
# for GOTCHA_LOG_CACHE_TTL seconds (0: off). Every read of the same container in that time is
# answered from the copy, so ten searches of a million-line log cost one fetch, not ten.
CACHE=${GOTCHA_LOG_CACHE:-/tmp/gotcha-logs}
TTL=${GOTCHA_LOG_CACHE_TTL:-180}
[[ $TTL =~ ^[0-9]+$ ]] || TTL=180
KEY="$CACHE/$HOST--$NAME"
WHOLE=0; { [ -n "$PAT" ] || [ "$N" = all ]; } && WHOLE=1

# Print what was asked for from a saved log: $1 the log, $2 its header line, $3 a note.
serve() {
  export LC_ALL=C
  printf '%s\n%s\n' "$2" "$3"
  if [ -n "$PAT" ]; then
    if [ "$N" = all ]; then
      echo "(lines matching /$PAT/ in the whole log; all shown)"
      grep -a -E -i -- "$PAT" "$1"
    else
      echo "(lines matching /$PAT/ in the whole log; last $N shown)"
      grep -a -E -i -- "$PAT" "$1" | tail -n "$N"
    fi
  elif [ "$N" = all ]; then cat "$1"
  else tail -n "$N" "$1"
  fi
}

if [ "$TTL" -gt 0 ] && [ -f "$KEY.log" ] && [ -f "$KEY.head" ]; then
  age=$(( $(date +%s) - $(stat -c %Y "$KEY.log") ))
  if [ "$age" -ge 0 ] && [ "$age" -lt "$TTL" ]; then
    serve "$KEY.log" "$(cat "$KEY.head")" \
      "(from a copy of the whole log fetched ${age}s ago; copies are kept ${TTL}s)"
    exit 0
  fi
fi

command -v tailscale >/dev/null || { echo "tailscale is not installed on this machine" >&2; exit 3; }
if ! pong=$(timeout 8 tailscale ping -c 1 --timeout 5s "$HOST" 2>&1); then
  echo "UNREACHABLE: no reply from $HOST ($(head -1 <<<"$pong"))"; exit 10
fi
. "$HERE/lib_ssh.sh"
pick_transport "$HOST" "$TARGET"
# A full fetch when this read wants the whole log and copies are on; otherwise the plain tail.
FETCH=0; [ "$WHOLE" = 1 ] && [ "$TTL" -gt 0 ] && FETCH=1

remote() {
  printf 'NAME=%q\nN=%q\nPAT=%q\nREAD=%q\nFETCH=%q\n' "$NAME" "$N" "$PAT" "$READ" "$FETCH"
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
if [ "$FETCH" = 1 ]; then
  # The whole log, then a marker with docker's exit code: the copy is kept only when the read
  # finished (a time limit or an error leaves a partial log that must not pass for the whole).
  timeout "$READ" "${D[@]}" logs -t "$c" 2>&1
  echo "__GOTCHA_LOG_END__ $?"
elif [ -n "$PAT" ]; then
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
}

if [ "$FETCH" = 0 ]; then
  remote | timeout "$LIMIT" "${SSH[@]}" 'bash -s' 2>&1 | sed -E -f "$HERE/redact.sed"
  rc=${PIPESTATUS[1]}
  [ "$rc" -eq 124 ] && echo "TIMED OUT after ${LIMIT}s talking to $TARGET via $LABEL"
  exit 0
fi

( umask 077; mkdir -p "$CACHE" ) && [ -O "$CACHE" ] && [ -w "$CACHE" ] \
  || { echo "cannot use the log cache $CACHE; set GOTCHA_LOG_CACHE_TTL=0" >&2; exit 3; }
find "$CACHE" -maxdepth 1 -type f -mmin +30 -delete 2>/dev/null      # old copies and leftovers
TMP=$(umask 077; mktemp "$KEY.XXXXXX") || exit 3
trap 'rm -f "$TMP" "$TMP.log"' EXIT
remote | timeout "$LIMIT" "${SSH[@]}" 'bash -s' 2>&1 | sed -E -f "$HERE/redact.sed" > "$TMP"
rc=${PIPESTATUS[1]}
head=$(head -1 "$TMP")
if [[ $head != "container: "* ]]; then
  cat "$TMP"                    # no docker, no single container, unreachable: say what it said
  [ "$rc" -eq 124 ] && echo "TIMED OUT after ${LIMIT}s talking to $TARGET via $LABEL"
  exit 0
fi
end=$(tail -1 "$TMP")
# Every docker line starts with its -t timestamp, so the marker can't be a log line.
sed -e '1d' -e '/^__GOTCHA_LOG_END__ [0-9]*$/d' "$TMP" > "$TMP.log"
if [ "$end" = "__GOTCHA_LOG_END__ 0" ]; then
  printf '%s\n' "$head" > "$KEY.head.$$" && mv -f "$KEY.head.$$" "$KEY.head"
  mv -f "$TMP.log" "$KEY.log"
  serve "$KEY.log" "$head" "(whole log fetched now; later reads of it within ${TTL}s use this copy)"
else
  # Cut short (time limit, docker error): answer from what arrived, say so, keep nothing.
  [[ $end == __GOTCHA_LOG_END__* ]] || end="no end marker"
  serve "$TMP.log" "$head" "(PARTIAL: the log read stopped early ($end; ssh exit $rc), so this is only the oldest part of the log and was not kept. For the newest part: remote_logs.sh $HOST $NAME 200000)"
fi
exit 0
