#!/usr/bin/env bash
# Keeps the console running: starts it, and starts it again if it stops. Started at boot by
# the user's crontab:
#
#   @reboot /home/gotcha/gotcha-support-agent/console/run_forever.sh
#
# Only one copy runs at a time (the lock), so starting this twice is harmless. To stop the
# console for good, kill this script first, then the console:
#
#   pkill -f console/run_forever.sh; pkill -f 'uvicorn console.server:app'
#
# Output goes to console/state/console.log.
set -uo pipefail
HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
mkdir -p "$HERE/state"
exec 9>"$HERE/state/console.lock"
flock -n 9 || { echo "the console is already kept running by another run_forever.sh" >&2; exit 0; }
exec >>"$HERE/state/console.log" 2>&1

while true; do
  echo "$(date -Is) starting the console"
  make -C "$HERE" run
  echo "$(date -Is) the console stopped (exit $?); starting it again in 5s"
  sleep 5
done
