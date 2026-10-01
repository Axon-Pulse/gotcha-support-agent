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
LOG_LINES=${GOTCHA_LOG_LINES:-30000}   # how far back each log scan reads
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
ss -ltnH 2>/dev/null | awk '{print $4}' | grep -E ':(8080|5173)$' | sort -u | sed 's/^/listening /' \
  || echo "(ss unavailable)"
LANIP=$(ip -4 -o addr show scope global 2>/dev/null | grep -vE 'tailscale|docker|br-|veth' | awk '{print $4}' | cut -d/ -f1 | head -1)
code() { curl -s -o /dev/null -m 5 -w '%{http_code}' "$1" 2>/dev/null; }
echo "gateway  127.0.0.1:8080/health -> $(code http://127.0.0.1:8080/health)"
[ -n "$LANIP" ] && echo "gateway  $LANIP:8080/health -> $(code "http://$LANIP:8080/health")   (000 here but 200 on loopback = gateway-bound-to-loopback)"
echo "frontend 127.0.0.1:5173/ -> $(code http://127.0.0.1:5173/)"
echo "gateway /health body: $(curl -s -m 5 http://127.0.0.1:8080/health 2>/dev/null | clip 600)"

scan_logs() {  # $1 = container
  local c=$1 combined hits first started lastts old all
  combined=$(printf '%s\n' "$SIGS" | grep -vE '^#|^$' | cut -d'|' -f2- | paste -sd'|')
  # One bounded read, not two full ones: a container with a huge log must not eat the time
  # budget (a Slack turn has 30s in total). The window is the newest lines.
  all=$(D logs -t --tail "$LOG_LINES" "$c" 2>&1)
  hits=$(printf '%s\n' "$all" | grep -E -i -- "$combined" | clip 400)
  first=$(printf '%s\n' "$all" | head -1 | cut -c1-30)
  started=$(D inspect -f '{{.State.StartedAt}}' "$c" 2>/dev/null | cut -c1-19)
  echo "(scanned the newest $LOG_LINES log lines, from $first; container last started ${started:-?})"
  [ -z "$hits" ] && { echo "no known signatures"; return; }
  printf '%s\n' "$SIGS" | grep -vE '^#|^$' | while IFS= read -r line; do
    slug=${line%%|*}; re=${line#*|}
    m=$(printf '%s\n' "$hits" | grep -E -i -- "$re")
    [ -z "$m" ] && continue
    lastts=$(printf '%s\n' "$m" | tail -1 | cut -d' ' -f1)
    old=""; [ -n "$started" ] && [[ "${lastts:0:19}" < "$started" ]] && old="  (OLD: last seen before the container's last start)"
    printf '[%s] x%s  first=%s  last=%s%s\n    %s\n' "$slug" "$(printf '%s\n' "$m" | wc -l)" \
      "$(printf '%s\n' "$m" | head -1 | cut -d' ' -f1)" "$lastts" "$old" \
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

# The newest status line of each sensor node, wherever it is in the log. The tail above can be
# all noise; `radar=` (the radar's own state) is the line that separates "link up" from "radar up".
if [ -n "$CORE" ]; then
  sec "sensor node status: latest line per node ($CORE, last 5000 log lines)"
  st=$(D logs --tail 5000 "$CORE" 2>&1 | grep -a -E '[A-Za-z0-9_]+: +[A-Z]+ +\| .*det [0-9.]+/s' \
    | awk '{ if (match($0, /[A-Za-z0-9_]+: +[A-Z]+ +\|/)) { n=substr($0, RSTART, index(substr($0,RSTART), ":")-1); last[n]=$0; if (!(n in ord)) { ord[n]=++k; name[k]=n } } }
           END { for (i=1; i<=k; i++) print last[name[i]] }' | clip 300)
  echo "${st:-no sensor status lines in the last 5000 log lines}"
fi

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
# A magos_radar `ip` is the radar's APU, not the radar. The radar itself is not in the config: it
# sits at the APU's address with .6x -> .5x (e.g. APU .60, radar .50), checked in "sensor paths".
APUS=""
[ -f "$SITECFG" ] && APUS=$(awk '/type:/{m=($0 ~ /magos_radar/)} m && match($0,/ip:[[:space:]]*"?[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+/){s=substr($0,RSTART,RLENGTH); sub(/ip:[[:space:]]*"?/,"",s); print s; m=0}' "$SITECFG" | sort -uV)
[ -n "$APUS" ] && echo "NOTE: magos_radar ip = the radar's APU ($(echo $APUS)), not the radar itself; the radar's own address is not in the config (see sensor paths)"

# --- the acoustic backend (ASU) -------------------------------------------------------------
# asu_node is told where its backend is (`-u URL`); that URL, not a port from the KB, is what
# must answer. A site can point it anywhere (localhost:9000, another host), so the check is
# built from the running node, else from the site config, and only then falls back to :8000.
sec "acoustic backend (ASU)"
ASU_URLS=$(ps -eo args= | grep -E '[a]su_node' | grep -oE -- '(-u|--[a-z-]*url)[ =]https?://[^ ]+' \
  | sed -E 's/^(-u|--[a-z-]+)[ =]//' | sort -u)
ASU_SRC="the running asu_node"
if [ -z "$ASU_URLS" ] && [ -f "$SITECFG" ]; then
  ASU_URLS=$(awk 'tolower($0) ~ /type:/ {m=(tolower($0) ~ /asu|acoustic|dumbo/)} m && match($0,/https?:\/\/[^ "]+/){print substr($0,RSTART,RLENGTH)}' "$SITECFG" | sort -u)
  [ -n "$ASU_URLS" ] && ASU_SRC="the site config"
fi
if [ -z "$ASU_URLS" ]; then
  echo "no asu_node running and no ASU url in the site config (does this site have an acoustic sensor?); trying the default :8000"
  CHECK_URLS=http://127.0.0.1:8000/
else
  echo "configured ASU url (from $ASU_SRC): $(echo $ASU_URLS)"
  CHECK_URLS=$ASU_URLS
fi
ASU_LISTEN=""; ASU_REMOTE=""
for u in $CHECK_URLS; do
  hp=${u#*://}; hp=${hp%%/*}; host=${hp%%:*}; port=${hp##*:}; [ "$port" = "$hp" ] && port=80
  echo "asu api  $u -> $(code "$u")   (from this machine)"
  [ -n "$CORE" ] && echo "asu api  $u -> $(D exec "$CORE" sh -c 'curl -s -o /dev/null -m 3 -w "%{http_code}" "$1" 2>/dev/null || echo "no curl in the container"' _ "$u" 2>/dev/null)   (from inside $CORE; this is where asu_node runs)"
  case $host in
    localhost|127.*|::1)
      l=$(ss -ltnH "sport = :$port" 2>/dev/null | awk '{print $4}' | paste -sd' ')
      echo "listeners on :$port: ${l:-none}"
      [ -n "$l" ] && ASU_LISTEN="$ASU_LISTEN $port" ;;
    *)
      ASU_REMOTE="$ASU_REMOTE $host"
      echo "$host is not this machine, so the backend is not a container here; route: $(ip route get "$host" 2>/dev/null | head -1 | grep -oE 'dev [^ ]+( src [^ ]+)?')" ;;
  esac
done
# What exists under an acoustic name, whether or not it runs. `docker ps -a` already shows
# exited containers; images, compose files, services and processes tell "stopped" from "never here".
ASU_PAT='dumbo|acoustic|(^|[^a-z])asu([^a-z]|$)'
found=""
c=$(D ps -a --format '{{.Names}} ({{.Status}})' 2>/dev/null | grep -iE "$ASU_PAT" | paste -sd';'); echo "containers named like it: ${c:-none}"; [ -n "$c" ] && found="$found container"
i=$(D images --format '{{.Repository}}:{{.Tag}} (created {{.CreatedSince}})' 2>/dev/null | grep -iE "$ASU_PAT" | head -5 | paste -sd';'); echo "images named like it: ${i:-none}"; [ -n "$i" ] && found="$found image"
f=$(timeout 4 find /home /opt -maxdepth 4 \( -name 'docker-compose*.y*ml' -o -name 'compose*.y*ml' \) -not -path '*/node_modules/*' 2>/dev/null \
    | head -60 | xargs -r grep -lisE "$ASU_PAT" 2>/dev/null | head -5 | paste -sd';'); echo "compose files that mention it: ${f:-none}"; [ -n "$f" ] && found="$found compose-file"
v=$(systemctl list-units --type=service --all --no-legend 2>/dev/null | grep -iE "$ASU_PAT" | awk '{print $1" "$3" "$4}' | head -5 | paste -sd';'); echo "systemd services named like it: ${v:-none}"; [ -n "$v" ] && found="$found service"
p=$(ps -eo pid=,etimes=,args= | grep -iE "$ASU_PAT" | grep -vE 'grep|asu_node|triage_remote' | clip 160 | head -5 | paste -sd';'); echo "other processes named like it: ${p:-none}"; [ -n "$p" ] && found="$found process"
if [ -n "$ASU_LISTEN" ]; then
  echo "acoustic backend: SOMETHING LISTENS on the configured port$ASU_LISTEN. If the http code above is 000 it accepts connections but does not answer HTTP; if it differs between 'this machine' and 'inside the container', the container cannot reach it."
elif [ -n "$ASU_REMOTE" ]; then
  echo "acoustic backend: configured on another host ($ASU_REMOTE); look at that host and the route above, not at containers here."
elif [ -n "$found" ]; then
  echo "acoustic backend: NOT LISTENING, but it exists on this machine ($found): stopped or not started. Look at its status above."
else
  echo "acoustic backend: NOT LISTENING, and nothing acoustic-named was found as a container, image, compose file, service or process. That shows it is not running here. It does not show it was never installed: it may live under another name or on another host. Do not call it 'not installed'."
fi
up_s=$(cut -d. -f1 /proc/uptime 2>/dev/null)
[ -n "$up_s" ] && [ "$up_s" -lt 3600 ] && echo "NOTE: the machine has been up only $(uptime -p 2>/dev/null). After a reboot, a backend that has not come back is a likelier story than one that was never installed."

# --- sensor paths ---------------------------------------------------------------------------
sec "sensor paths (route taken, neighbour cache$([ "$PING_SENSORS" = 1 ] && echo ', ping'))"
IPS=$(printf '%s\n' "$PC" | grep -oE '\b(10\.[0-9]+|172\.(1[6-9]|2[0-9]|3[01])|192\.168)\.[0-9]+\.[0-9]+\b' | sort -uV | head -40)
[ -z "$IPS" ] && echo "no sensor addresses found in resolved config"
# Pings run in parallel and each is capped, so a site with several dead sensors costs a few
# seconds in total, not ~3s per sensor one after another.
PINGD=$(mktemp -d 2>/dev/null)
RADARS=""
for apu in $APUS; do last=${apu##*.}; [[ $last =~ ^6[0-9]$ ]] && RADARS="$RADARS ${apu%.*}.5${last:1}"; done
if [ "$PING_SENSORS" = 1 ] && [ -n "$PINGD" ]; then
  for ip in $IPS $RADARS; do
    ( timeout 5 ping -c2 -W1 "$ip" >/dev/null 2>&1 && echo "ping OK" || echo "ping DOWN" ) > "$PINGD/$ip" &
  done
  wait
fi
sensor_line() {  # $1 = ip, $2 = label
  local ip=$1 r n p="" flag=""
  r=$(ip route get "$ip" 2>/dev/null | head -1 | grep -oE 'dev [^ ]+( src [^ ]+)?')
  n=$(ip neigh show "$ip" 2>/dev/null | awk '{print $NF}')
  [ "$PING_SENSORS" = 1 ] && p=$(cat "$PINGD/$ip" 2>/dev/null || echo "ping not run")
  [[ $r == *tailscale* ]] && flag="  <-- ROUTED VIA TAILNET (hijack)"
  printf '%-16s %-32s neigh=%-10s %s%s  %s\n' "$ip" "${r:-no route}" "${n:-none}" "$p" "$flag" "$2"
}
for ip in $IPS; do
  lab=""; grep -qxF "$ip" <<<"$APUS" && lab="<- magos APU (config ip)"
  sensor_line "$ip" "$lab"
done
# The radar behind each APU: same address, .6x -> .5x. Derived, not configured. An APU that
# answers while this address does not is the signature of a radar that is not connected.
for apu in $APUS; do
  last=${apu##*.}
  if [[ $last =~ ^6[0-9]$ ]]; then
    radar="${apu%.*}.5${last:1}"
    sensor_line "$radar" "<- magos RADAR (derived from APU $apu, not in config)"
  else
    echo "$apu: APU does not end in .6x, so the radar address can't be derived; ask which address the radar has"
  fi
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
