# Sourced by run_triage.sh and remote_logs.sh. Not a command: the Slack guard does not allow
# running it.
#
# pick_transport <host> <target>  sets SSH (the command array) and LABEL.
pick_transport() {
  local HOST=$1 TARGET=$2
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
}
