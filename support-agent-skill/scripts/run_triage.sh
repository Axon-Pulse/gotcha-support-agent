#!/usr/bin/env bash
# Run the read-only triage bundle on a gotcha machine in one SSH round-trip.
#
#   run_triage.sh <tailnet-host> [--ping]
#
# --ping also pings each sensor address from the resolved config. It puts traffic on the
# customer's sensor subnets, so only pass it once someone has agreed to that.
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
OUT_DIR=${GOTCHA_TRIAGE_DIR:-/tmp/gotcha-triage}
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

# Transport: tailscale ssh only works when the peer runs Tailscale SSH (it then advertises
# host keys on the tailnet). Otherwise use plain ssh over the tailnet name, which honours
# ~/.ssh/config. BatchMode makes a missing key fail fast instead of hanging on a password.
# GOTCHA_SSH=tailscale|ssh forces one.
TRANSPORT=${GOTCHA_SSH:-}
if [ -z "$TRANSPORT" ]; then
  TRANSPORT=$(tailscale status --json 2>/dev/null | python3 -c '
import json, sys
h = sys.argv[1].lower()
for p in (json.load(sys.stdin).get("Peer") or {}).values():
    names = {(p.get("DNSName") or "").split(".")[0].lower(), (p.get("HostName") or "").lower()}
    if h in names or h == (p.get("DNSName") or "").rstrip(".").lower():
        print("tailscale" if p.get("sshHostKeys") else "ssh"); break
else:
    print("ssh")
' "$HOST")
fi
LABEL=$([ "$TRANSPORT" = tailscale ] && echo "tailscale ssh" || echo "plain ssh")
case $TRANSPORT in
  tailscale) SSH=(tailscale ssh "$TARGET") ;;
  *)         SSH=(ssh -o BatchMode=yes -o ConnectTimeout=15 "$TARGET") ;;
esac
echo "connecting to $TARGET via $LABEL" >&2

{
  printf 'PING_SENSORS=%s\n' "$PING"
  printf 'CFG_OVERRIDE=%q\n' "${GOTCHA_CONFIG:-}"
  printf "SIGS=\$(cat <<'__GOTCHA_SIGS__'\n"
  cat "$HERE/signatures.txt"
  printf '__GOTCHA_SIGS__\n)\n'
  cat "$HERE/triage_remote.sh"
} | timeout "$LIMIT" "${SSH[@]}" 'bash -s' 2>&1 | tee "$OUT"
rc=${PIPESTATUS[1]}

echo
if [ "$rc" -eq 124 ]; then
  echo "TIMED OUT after ${LIMIT}s talking to $TARGET via $LABEL (weak link, or tailscale ssh waiting for browser check-mode auth). Partial output above is still valid."
elif [ "$rc" -ne 0 ]; then
  echo "$LABEL to $TARGET exited $rc"
fi
echo "saved: $OUT"

# Remember what this system runs, so code lookups and answers still know its version when
# the machine is unreachable later. One row per host: host, when, release, image tag, config.
if grep -q '^=== end of triage ===' "$OUT"; then
  STATE="$HERE/../state/systems.tsv"; mkdir -p "$(dirname "$STATE")"
  rel=$(grep -m1 '^release: ' "$OUT" | cut -d' ' -f2)
  tag=$(grep -m1 '^IMAGE_TAG=' "$OUT" | cut -d= -f2)
  cfg=$(grep -m1 '^config: ' "$OUT" | cut -d' ' -f2)
  { [ -f "$STATE" ] && awk -F'\t' -v h="$HOST" '$1!=h' "$STATE"
    printf '%s\t%s\t%s\t%s\t%s\n' "$HOST" "$(date -u +%FT%TZ)" "${rel:-unknown}" "${tag:-unknown}" "${cfg:-UNKNOWN}"
  } > "$STATE.tmp" && mv "$STATE.tmp" "$STATE"
fi
