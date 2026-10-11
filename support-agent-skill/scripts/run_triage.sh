#!/usr/bin/env bash
# Run the read-only triage bundle on a gotcha machine in one SSH round-trip.
#
#   run_triage.sh <tailnet-host> [--ping]
#
# --ping also pings each sensor address from the resolved config. It puts traffic on the
# customer's sensor subnets, so from a terminal only pass it once someone has agreed to that.
# The Slack bot has that agreement built in (BRIDGE_ALLOW_PING, on by default) and passes it
# on the first run for radar / sensor / no-detections reports.
# Set GOTCHA_SSH_USER to log in as a specific user (default: tailscale ssh's own default).
# Set GOTCHA_CONFIG=configs/<...> when no launcher is running and the site config can't be
# guessed (the triage output says so).
# The output is also saved under $GOTCHA_TRIAGE_DIR (default /tmp/gotcha-triage) so it can
# be attached to an escalation.
set -uo pipefail

[ $# -ge 1 ] || { echo "usage: $0 <tailnet-host> [--ping]" >&2; exit 2; }
HOST=$1; shift
PING=0; [ "${1:-}" = "--ping" ] && PING=1
HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
TARGET="${GOTCHA_SSH_USER:+$GOTCHA_SSH_USER@}$HOST"
# Where the per-system memory (host, release, config, gotcha dir) lives. Outside the skill
# folder, which can be read-only or replaced on update. Shared with code.sh.
STATE_DIR=${GOTCHA_STATE_DIR:-${XDG_STATE_HOME:-$HOME/.local/state}/gotcha-support}
STATE="$STATE_DIR/systems.tsv"
OLD_STATE="$HERE/../state/systems.tsv"   # where earlier versions kept it; copied over once
if [ ! -f "$STATE" ] && [ -f "$OLD_STATE" ]; then mkdir -p "$STATE_DIR" 2>/dev/null && cp "$OLD_STATE" "$STATE" 2>/dev/null; fi
OUT_DIR=${GOTCHA_TRIAGE_DIR:-/tmp/gotcha-triage}
umask 077   # the saved triage is for the person escalating, not for other users on this machine
mkdir -p "$OUT_DIR"
OUT="$OUT_DIR/$HOST-$(date +%Y%m%d-%H%M%S).txt"
LIMIT=${GOTCHA_TRIAGE_TIMEOUT:-120}   # whole triage; a healthy link finishes well inside this

command -v tailscale >/dev/null || { echo "tailscale is not installed on this machine" >&2; exit 3; }

# Fail in seconds, not minutes: one tailnet ping before committing to the triage. On no
# reply, exit 10 with the version to use for code lookups instead (the last recorded release,
# else the newest release), so the caller can answer from the KB and source without waiting.
if ! pong=$(timeout 8 tailscale ping -c 1 --timeout 5s "$HOST" 2>&1); then
  if grep -qiE 'stopped|not running|NeedsLogin|logged out' <<<"$pong"; then
    echo "UNREACHABLE: Tailscale on THIS machine isn't running ($pong). Fix locally: sudo tailscale up"
  else
    echo "UNREACHABLE: no reply from $HOST within 5s ($(head -1 <<<"$pong"))"
  fi
  echo "fallback version for code lookups: $("$HERE/code.sh" resolve "$HOST" 2>&1 >/dev/null | paste -sd' ')"
  exit 10
fi
echo "$(head -1 <<<"$pong")" >&2   # 'via DERP(...)' here means a relayed, slower link

. "$HERE/lib_ssh.sh"
pick_transport "$HOST" "$TARGET"
echo "connecting to $TARGET via $LABEL" >&2

{
  printf 'PING_SENSORS=%s\n' "$PING"
  printf 'CFG_OVERRIDE=%q\n' "${GOTCHA_CONFIG:-}"
  printf "SIGS=\$(cat <<'__GOTCHA_SIGS__'\n"
  cat "$HERE/signatures.txt"
  printf '__GOTCHA_SIGS__\n)\n'
  cat "$HERE/triage_remote.sh"
} | timeout "$LIMIT" "${SSH[@]}" 'bash -s' 2>&1 | sed -E -f "$HERE/redact.sed" | tee "$OUT"
rc=${PIPESTATUS[1]}

echo
if [ "$rc" -eq 124 ]; then
  echo "TIMED OUT after ${LIMIT}s talking to $TARGET via $LABEL (weak link, or tailscale ssh waiting for browser check-mode auth). Partial output above is still valid."
elif [ "$rc" -ne 0 ]; then
  echo "$LABEL to $TARGET exited $rc"
fi
echo "saved: $OUT"

# Remember what this system runs, so code lookups and answers still know its version when
# the machine is unreachable later. One row per host: host, when, release, image tag, config,
# gotcha dir (absolute, on the machine; the config path above is relative to it).
# A run cut short by the time limit has no "end of triage" line but still has the release,
# which is what the offline fallback needs, so the release line decides, not the end marker.
rel=$(grep -m1 '^release: ' "$OUT" | cut -d' ' -f2)
if [ -n "$rel" ] && [ "$rel" != unknown ]; then
  mkdir -p "$STATE_DIR"
  tag=$(grep -m1 '^IMAGE_TAG=' "$OUT" | cut -d= -f2)
  cfg=$(grep -m1 '^config: ' "$OUT" | cut -d' ' -f2)
  gdir=$(grep -m1 '^dir: ' "$OUT" | cut -d' ' -f2-)
  { [ -f "$STATE" ] && awk -F'\t' -v h="$HOST" '$1!=h' "$STATE"
    printf '%s\t%s\t%s\t%s\t%s\t%s\n' "$HOST" "$(date -u +%FT%TZ)" "$rel" "${tag:-unknown}" "${cfg:-UNKNOWN}" "${gdir:-unknown}"
  } > "$STATE.tmp" && mv "$STATE.tmp" "$STATE"
fi
