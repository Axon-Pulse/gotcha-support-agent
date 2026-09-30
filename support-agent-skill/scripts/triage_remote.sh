#!/usr/bin/env bash
# Runs ON the gotcha machine. run_triage.sh feeds it over `tailscale ssh ... bash -s`, with
# SIGS (the contents of signatures.txt) and PING_SENSORS prepended.
#
# Read-only by construction: it inspects containers, processes, ports, logs and the resolved
# config, and writes nothing but stdout. It never restarts, edits, pulls or builds anything.
# Secrets are kept out of the output: .env is read for key names (plus IMAGE_TAG/MODE) only,
# and resolved config is redacted before printing.

export LC_ALL=C
SIGS=${SIGS:-}
PING_SENSORS=${PING_SENSORS:-0}
shopt -s nullglob nocaseglob

sec()   { printf '\n=== %s ===\n' "$*"; }
clip()  { cut -c1-"${1:-300}"; }
have()  { command -v "$1" >/dev/null 2>&1; }
redact() {
  sed -E \
    -e 's#(://)[^/@[:space:]]+:[^/@[:space:]]+@#\1<redacted>@#g' \
    -e 's/((password|passwd|pass|token|secret|api_?key|credential)[A-Za-z0-9_]*["'\'']?[[:space:]]*[:=][[:space:]]*).*/\1<redacted>/I'
}

# --- docker access (group membership, else passwordless sudo, else none) -------------------
if docker info >/dev/null 2>&1; then DOCKER=(docker)
elif sudo -n docker info >/dev/null 2>&1; then DOCKER=(sudo -n docker)
else DOCKER=()
fi
D() { [ ${#DOCKER[@]} -gt 0 ] && timeout "${DT:-30}" "${DOCKER[@]}" "$@"; }

container_for() {  # compose service name -> container name
  local c
  c=$(D ps -a --filter "label=com.docker.compose.service=$1" --format '{{.Names}}' 2>/dev/null | head -1)
  [ -z "$c" ] && c=$(D ps -a --format '{{.Names}}' 2>/dev/null | grep -i -m1 -- "$1")
  printf '%s' "$c"
}
CORE=$(container_for gotcha30)
GW=$(container_for gateway)

# --- gotcha directory: compose's own record first, then the usual places -------------------
GDIR=""
for c in "$CORE" "$GW"; do
  [ -n "$c" ] || continue
  GDIR=$(D inspect -f '{{index .Config.Labels "com.docker.compose.project.working_dir"}}' "$c" 2>/dev/null)
  [ -d "$GDIR" ] && break || GDIR=""
done
if [ -z "$GDIR" ]; then
  for d in "$HOME"/{deploy,gotcha}* "$HOME"/Documents/{deploy,gotcha}* "$HOME"/*/{deploy,gotcha}* \
           /home/*/{deploy,gotcha}* /home/*/Documents/{deploy,gotcha}* /opt/{deploy,gotcha}*; do
    [ -f "$d/Makefile" ] || continue
    [ -f "$d/docker-compose.yml" ] || [ -f "$d/docker-compose.yaml" ] || [ -f "$d/compose.yaml" ] || continue
    GDIR=$d; break
  done
fi

# =========================================================================================
sec host
echo "hostname: $(hostname)   now(utc): $(date -u +%FT%TZ)   up: $(uptime -p 2>/dev/null)"
[ ${#DOCKER[@]} -eq 0 ] && echo "WARNING: no docker access for $(id -un) (not in docker group, no passwordless sudo) — container sections will be empty"

sec "gotcha dir + version"
if [ -n "$GDIR" ]; then
  echo "dir: $GDIR"
  if [ -f "$GDIR/.env" ]; then
    grep -E '^(IMAGE_TAG|MODE)=' "$GDIR/.env" || echo "IMAGE_TAG/MODE: not set in .env"
    if [ -f "$GDIR/.env.example" ]; then
      keys() { grep -oE '^[A-Z_][A-Z0-9_]*=' "$1" | sed 's/=$//' | sort -u; }
      missing=$(comm -23 <(keys "$GDIR/.env.example") <(keys "$GDIR/.env") | paste -sd' ')
      echo "keys in .env.example missing from .env: ${missing:-none}"
    fi
  else
    echo ".env: MISSING"
  fi
else
  echo "gotcha dir: NOT FOUND (no compose working_dir label; nothing under ~/deploy*, ~/gotcha*, ~/Documents/...)"
fi
# The exact source version is compiled into the launcher (git describe at build time, e.g.
# v1.3.0-38-g7c9b0167). IMAGE_TAG alone doesn't pin it: `stable` moves with every release.
REL=""
if [ -n "$CORE" ]; then
  REL=$(D exec "$CORE" sh -c 'L=./build/bin/system_launcher; [ -x "$L" ] || L=$(command -v system_launcher); [ -n "$L" ] && "$L" --version' 2>/dev/null \
        | grep -oE 'v[0-9][^ ]*' | head -1)
  [ -z "$REL" ] && REL=$(D logs --tail 20000 "$CORE" 2>&1 | grep -a -oE 'Release: [^ ]+' | tail -1 | cut -d' ' -f2)
  echo "image: $(D inspect -f '{{.Config.Image}}  id={{.Image}}' "$CORE" 2>/dev/null | cut -c1-100)"
fi
echo "release: ${REL:-unknown}"

sec containers
D ps -a --format '{{.Names}}\t{{.Label "com.docker.compose.service"}}\t{{.Status}}\t{{.Image}}' 2>/dev/null \
  || echo "(docker ps unavailable)"
for c in $(D ps -a --format '{{.Names}}' 2>/dev/null); do
  D inspect -f "$c"' restarts={{.RestartCount}} started={{.State.StartedAt}} exit={{.State.ExitCode}} oom_killed={{.State.OOMKilled}}' "$c" 2>/dev/null
done

sec "launcher sessions + node processes (etime = seconds alive)"
mapfile -t LAUNCHERS < <(ps -eo pid=,etimes=,args= | grep -E '[s]ystem_launcher' | grep -v -- '--print-config')
echo "system_launcher processes: ${#LAUNCHERS[@]}$([ ${#LAUNCHERS[@]} -gt 1 ] && echo '   <-- MORE THAN ONE: see two-launcher-sessions')"
for l in "${LAUNCHERS[@]}"; do
  echo "launcher: $(echo "$l" | clip 250)"
  lp=$(echo "$l" | awk '{print $1}')
  ps -o pid=,etimes=,stat=,args= --ppid "$lp" 2>/dev/null | clip 200 | sed 's/^/    /'
done

sec "listeners + http checks"
ss -ltnH 2>/dev/null | awk '{print $4}' | grep -E ':(8080|5173|8000)$' | sort -u | sed 's/^/listening /' \
  || echo "(ss unavailable)"
LANIP=$(ip -4 -o addr show scope global 2>/dev/null | grep -vE 'tailscale|docker|br-|veth' | awk '{print $4}' | cut -d/ -f1 | head -1)
code() { curl -s -o /dev/null -m 5 -w '%{http_code}' "$1" 2>/dev/null; }
echo "gateway  127.0.0.1:8080/health -> $(code http://127.0.0.1:8080/health)"
[ -n "$LANIP" ] && echo "gateway  $LANIP:8080/health -> $(code "http://$LANIP:8080/health")   (000 here but 200 on loopback = gateway-bound-to-loopback)"
echo "frontend 127.0.0.1:5173/ -> $(code http://127.0.0.1:5173/)"
echo "asu api  127.0.0.1:8000/ -> $(code http://127.0.0.1:8000/)"
echo "gateway /health body: $(curl -s -m 5 http://127.0.0.1:8080/health 2>/dev/null | clip 600)"

scan_logs() {  # $1 = container
  local c=$1 combined hits first
  combined=$(printf '%s\n' "$SIGS" | grep -vE '^#|^$' | cut -d'|' -f2- | paste -sd'|')
  hits=$(DT=120 D logs -t "$c" 2>&1 | grep -E -i -- "$combined" | clip 400)
  first=$(D logs -t "$c" 2>&1 | head -1 | cut -c1-30)
  echo "(log starts $first)"
  [ -z "$hits" ] && { echo "no known signatures"; return; }
  printf '%s\n' "$SIGS" | grep -vE '^#|^$' | while IFS= read -r line; do
    slug=${line%%|*}; re=${line#*|}
    m=$(printf '%s\n' "$hits" | grep -E -i -- "$re")
    [ -z "$m" ] && continue
    printf '[%s] x%s  first=%s  last=%s\n    %s\n' "$slug" "$(printf '%s\n' "$m" | wc -l)" \
      "$(printf '%s\n' "$m" | head -1 | cut -d' ' -f1)" "$(printf '%s\n' "$m" | tail -1 | cut -d' ' -f1)" \
      "$(printf '%s\n' "$m" | tail -1 | cut -d' ' -f2- | clip 300)"
  done
}
for c in "$CORE" "$GW" $(D ps -a --format '{{.Names}}' 2>/dev/null | grep -i dumbo); do
  [ -n "$c" ] || continue
  sec "log signatures: $c"
  scan_logs "$c"
done

[ -n "$CORE" ] && { sec "log tail: $CORE (last 40)"; D logs --tail 40 "$CORE" 2>&1 | clip 250; }
[ -n "$GW" ]   && { sec "log tail: $GW (last 25)";   D logs --tail 25 "$GW"   2>&1 | clip 250; }

# --- resolved config ------------------------------------------------------------------------
sec "resolved config (--print-config, redacted)"
# Which config: an explicit override, else what the running launcher was given, else a guess
# from the site configs on disk (per-system dirs, or the older flat configs/*.yaml layout).
CFG=${CFG_OVERRIDE:-}
[ -z "$CFG" ] && CFG=$(printf '%s\n' "${LAUNCHERS[@]}" | grep -oE -- '(-c|--config)[ =][^ ]+' | head -1 | sed -E 's/^(-c|--config)[ =]//')
if [ -z "$CFG" ] && [ -n "$GDIR" ]; then
  cands=("$GDIR"/configs/*/full_system.yaml)
  if [ ${#cands[@]} -gt 0 ]; then
    site=()
    for f in "${cands[@]}"; do [[ $f == */default/* ]] || site+=("${f#"$GDIR"/}"); done
    [ ${#site[@]} -eq 1 ] && CFG=${site[0]}
  else
    cands=("$GDIR"/configs/*.yaml)
  fi
  echo "(no running launcher; configs on disk: $(printf '%s\n' "${cands[@]}" | sed "s#$GDIR/##" | paste -sd' ' | clip 600))"
  [ -z "$CFG" ] && echo "(cannot tell which one this site uses — re-run with a config: GOTCHA_CONFIG=configs/<x> run_triage.sh <host>)"
fi
echo "config: ${CFG:-UNKNOWN}"
PC=""; pc_ok=1
if [ -n "$CFG" ]; then
  # The host binary is often present but unrunnable (its libs live in the image), so fall
  # back to the core container whenever the host attempt fails.
  if [ -n "$GDIR" ] && [ -x "$GDIR/build/bin/system_launcher" ]; then
    PC=$(cd "$GDIR" && timeout 20 ./build/bin/system_launcher -c "$CFG" --print-config 2>&1); pc_ok=$?
    [ $pc_ok -eq 0 ] && echo "(ran on host)"
  fi
  if [ $pc_ok -ne 0 ] && [ -n "$CORE" ]; then
    PC=$(D exec "$CORE" sh -c 'L=./build/bin/system_launcher; [ -x "$L" ] || L=$(command -v system_launcher); [ -n "$L" ] && exec "$L" -c "$1" --print-config' _ "$CFG" 2>&1); pc_ok=$?
    echo "(ran in $CORE, exit $pc_ok)"
  fi
fi
if [ -n "$PC" ]; then printf '%s\n' "$PC" | redact | clip 220 | head -250
else echo "could not run --print-config"; fi
if [ -n "$GDIR" ] && [ -n "$CFG" ]; then
  site_dir=$(dirname "$GDIR/$CFG")
  grep -rn 'SET THIS' "$site_dir" 2>/dev/null | head -5 | sed 's/^/placeholder never set: /'
  grep -n 'end_on_first_complete: *true' "$GDIR/$CFG" 2>/dev/null | sed 's|^|end_on_first_complete is TRUE (one node restart resets the whole system, see end-on-first-complete-resets-system): |'
fi
printf '%s\n' "$PC" | grep -q '32\.7767' && echo "NOTE: Dallas coordinates (32.7767) in resolved config"

# --- sensor addresses, straight from the site's own config ---------------------------------
# The only trustworthy source for a sensor's address. The config lives on the host (bind-mounted
# into the core container at /app/configs); nothing here comes from the KB or the defaults.
sec "site config: sensor addresses (node, type, address; from $([ -n "$CFG" ] && echo "$CFG" || echo '?'))"
SITECFG="$GDIR/${CFG#/app/}"   # the launcher may have been given the in-container path
if [ -n "$GDIR" ] && [ -f "$SITECFG" ]; then
  echo "file: $SITECFG"
  grep -nE '^  /|^    [A-Za-z0-9_-]+:[[:space:]]*$|type:|\b(ip|base_url|host|address)\b *:' "$SITECFG" | redact | clip 160
else
  echo "site config not readable on the host ($SITECFG); the resolved config above is what the launcher loaded"
fi

# --- sensor paths ---------------------------------------------------------------------------
sec "sensor paths (route taken, neighbour cache$([ "$PING_SENSORS" = 1 ] && echo ', ping'))"
IPS=$(printf '%s\n' "$PC" | grep -oE '\b(10\.[0-9]+|172\.(1[6-9]|2[0-9]|3[01])|192\.168)\.[0-9]+\.[0-9]+\b' | sort -uV | head -40)
[ -z "$IPS" ] && echo "no sensor addresses found in resolved config"
for ip in $IPS; do
  r=$(ip route get "$ip" 2>/dev/null | head -1 | grep -oE 'dev [^ ]+( src [^ ]+)?')
  n=$(ip neigh show "$ip" 2>/dev/null | awk '{print $NF}')
  p=""
  [ "$PING_SENSORS" = 1 ] && { ping -c2 -W2 "$ip" >/dev/null 2>&1 && p="ping OK" || p="ping DOWN"; }
  flag=""; [[ $r == *tailscale* ]] && flag="  <-- ROUTED VIA TAILNET (hijack)"
  printf '%-16s %-32s neigh=%-10s %s%s\n' "$ip" "${r:-no route}" "${n:-none}" "$p" "$flag"
done
tsr=$(ip route show table 52 2>/dev/null | grep -vE '^(100\.|fd7a|throw|unreachable)' | head -10)
[ -n "$tsr" ] && { echo "tailscale-installed routes to non-tailnet subnets:"; echo "$tsr" | sed 's/^/    /'; }

# --- gateway config, model weights, ffmpeg --------------------------------------------------
sec "gateway config (host/port/mode lines)"
gcfg=""
for f in "$GDIR"/gateway-config/config.yaml "$GDIR"/GUItcha30/config/config.yaml; do [ -f "$f" ] && { gcfg=$f; break; }; done
if [ -n "$gcfg" ]; then echo "$gcfg"; grep -nE '^\s*(host|port|mode|allowed_websocket_hosts)\s*:' "$gcfg" | redact
else echo "not found on host"; fi
[ -n "$GDIR" ] && grep -E '^GATEWAY_HOST=' "$GDIR/.env" 2>/dev/null

sec "model weights + ffmpeg"
if [ -n "$CORE" ]; then
  echo "weights ($CORE): $(D exec "$CORE" sh -c 'ls python/models/weights 2>&1' | paste -sd' ' | clip 300)"
  echo "ffmpeg  ($CORE): $(D exec "$CORE" sh -c 'command -v ffmpeg || echo MISSING' 2>&1 | head -1)"
fi
[ -n "$GW" ] && echo "ffmpeg  ($GW): $(D exec "$GW" sh -c 'command -v ffmpeg || echo MISSING' 2>&1 | head -1)"
[ -n "$GDIR" ] && [ -d "$GDIR/python/models/weights" ] && echo "weights (host): $(ls "$GDIR/python/models/weights" | paste -sd' ')"

sec resources
df -h / /var/lib/docker 2>/dev/null | awk 'NR==1 || !seen[$0]++'
free -h 2>/dev/null | head -2
echo "/dev/shm ecal segments: $(ls /dev/shm 2>/dev/null | grep -c ecal)"
oom=$( (dmesg -T 2>/dev/null || journalctl -k -q --since '-7d' 2>/dev/null) | grep -i 'killed process' | tail -3)
echo "recent OOM kills: ${oom:-none seen (or kernel log not readable)}"

sec "end of triage"
