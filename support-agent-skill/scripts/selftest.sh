#!/usr/bin/env bash
# Offline checks for the skill's own scripts and KB. No machine needed: a stub `tailscale`
# stands in for the tailnet. Run it after editing any script, signatures.txt or a case.
#
#   scripts/selftest.sh
set -uo pipefail
HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
SKILL=$(dirname "$HERE")
fail=0
ok()  { printf 'ok    %s\n' "$1"; }
bad() { printf 'FAIL  %s\n' "$1"; fail=1; }

# --- redaction ---------------------------------------------------------------------------
red() { printf '%s\n' "$1" | sed -E -f "$HERE/redact.sed"; }
for s in 'camera_password=hunter2' 'pwd: hunter2' 'camera_pw=hunter2' 'Authorization: Bearer abc123def456' \
         'rtsp://admin:hunter2@10.0.0.5/stream' 'x-cli --password hunter2' '"api_key": "hunter2"' 'token=hunter2'; do
  [[ "$(red "$s")" == *hunter2* || "$(red "$s")" == *abc123def456* ]] && bad "redact leaks: $s" || ok "redacts: $s"
done
for s in 'compass: north' 'asu_connected: false' 'load_model: loaded model' 'First node completed: magos2'; do
  [ "$(red "$s")" = "$s" ] && ok "leaves alone: $s" || bad "redact over-reaches: $s -> $(red "$s")"
done

# --- list_systems.sh: "gotcha 3" must not find gotcha-30 -------------------------------------
STUB=$(mktemp -d); trap 'rm -rf "$STUB"' EXIT
cat > "$STUB/tailscale" <<'EOS'
#!/usr/bin/env bash
cat <<'JSON'
{"Peer": {
 "a": {"HostName":"axon-gotcha-3",  "DNSName":"axon-gotcha-3.tail.ts.net.",  "TailscaleIPs":["100.0.0.3"],  "Online":false, "LastSeen":"2026-09-30T10:00:00Z"},
 "b": {"HostName":"axon-gotcha-30", "DNSName":"axon-gotcha-30.tail.ts.net.", "TailscaleIPs":["100.0.0.30"], "Online":true},
 "c": {"HostName":"axon-gotcha-4",  "DNSName":"axon-gotcha-4.tail.ts.net.",  "TailscaleIPs":["100.0.0.4"],  "Online":true}}}
JSON
EOS
chmod +x "$STUB/tailscale"
out=$(PATH="$STUB:$PATH" "$HERE/list_systems.sh" gotcha 3)
grep -q 'axon-gotcha-3 ' <<<"$out" && ! grep -q 'gotcha-30' <<<"$out" && ok "gotcha 3 matches gotcha-3 only" || bad "gotcha 3 matched the wrong machine: $out"
grep -q 'every match is offline' <<<"$out" && ok "offline-only match is called out" || bad "offline-only match not called out"
out=$(PATH="$STUB:$PATH" "$HERE/list_systems.sh" gotcha 30)
grep -q 'gotcha-30' <<<"$out" && ! grep -q 'gotcha-3 ' <<<"$out" && ok "gotcha 30 matches gotcha-30 only" || bad "gotcha 30: $out"

# --- every script parses; remote_logs.sh refuses bad input before it touches the network -----
for f in "$HERE"/*.sh; do bash -n "$f" 2>/dev/null && ok "parses: $(basename "$f")" || bad "syntax error: $(basename "$f")"; done
for args in "h" "h 'a;b'" "h dumbo 0" "h dumbo all x" "h dumbo 10 -f" "h dumbo --grep" "h dumbo 5 x"; do
  eval "\"$HERE/remote_logs.sh\" $args" >/dev/null 2>&1; [ $? -eq 2 ] && ok "remote_logs refuses: $args" || bad "remote_logs accepted: $args"
done

# --- triage network section: feed it a fake NIC tree ------------------------------------------
NET=$(mktemp -d); trap 'rm -rf "$STUB" "$NET"' EXIT
mknic() {  # name state speed duplex crc ups [wifi]
  mkdir -p "$NET/$1/statistics" "$NET/$1/device"
  printf '%s\n' "$2" > "$NET/$1/operstate"; printf '%s\n' "$3" > "$NET/$1/speed"; printf '%s\n' "$4" > "$NET/$1/duplex"
  printf '%s\n' "$5" > "$NET/$1/statistics/rx_crc_errors"; printf '0\n' > "$NET/$1/statistics/rx_errors"
  printf '0\n' > "$NET/$1/statistics/tx_errors"; printf '%s\n' "$6" > "$NET/$1/carrier_up_count"
  [ -n "${7:-}" ] && mkdir -p "$NET/$1/wireless"
}
mknic eth0 up 10 half 12 5          # the bad cable: negotiated down, errors, flapping
mknic eth1 up 1000 full 0 1         # healthy
mknic eth2 down -1 unknown 0 1      # unplugged
mknic wlan0 up -1 unknown 0 2 wifi  # weak wifi
mkdir -p "$NET/docker0"             # virtual: no device link, must not be listed
printf 'Inter-| sta-|   Quality        |   Discarded packets               | Missed | WE\n face | tus | link level noise |  nwid  crypt   frag  retry   misc | beacon | 22\n wlan0: 0000   28.  -78.  -256        0      0      0      0      0        0\n' > "$NET/wireless"
net=$( { printf 'PING_SENSORS=0\nGWS=""\nclip() { cut -c1-300; }\nsec() { printf "=== %%s ===\\n" "$*"; }\nNETSYS=%q\nPROC_WIRELESS=%q\ndeclare -A PINGRES\n' "$NET" "$NET/wireless"
       sed -n '/^# --- network links/,/^# --- gateway config/p' "$HERE/triage_remote.sh" | sed '$d'; } | bash 2>&1 )
grep -E '^eth0 ' <<<"$net" | grep -q 'BELOW 1000' && ok "net: 10 Mbit link is flagged" || bad "net: 10 Mbit not flagged: $net"
grep -E '^eth0 ' <<<"$net" | grep -q 'HALF duplex' && ok "net: half duplex is flagged" || bad "net: half duplex not flagged"
grep -E '^eth0 ' <<<"$net" | grep -q '12 CRC errors' && ok "net: CRC errors are flagged" || bad "net: CRC errors not flagged"
grep -E '^eth0 ' <<<"$net" | grep -q 'came up 5 times' && ok "net: flapping is flagged" || bad "net: flapping not flagged"
grep -E '^eth1 ' <<<"$net" | grep -q '<--' && bad "net: healthy gigabit link flagged" || ok "net: healthy gigabit link not flagged"
grep -E '^eth2 ' <<<"$net" | grep -q 'no link' && ok "net: down port reported as no link" || bad "net: down port: $net"
grep -E '^wlan0 ' <<<"$net" | grep -q 'WEAK SIGNAL' && ok "net: weak wifi is flagged" || bad "net: weak wifi not flagged: $net"
grep -q '^docker0' <<<"$net" && bad "net: virtual device listed" || ok "net: virtual device skipped"

# --- remote_logs.sh keeps one redacted copy of a whole log and answers repeat reads from it ------
cat > "$STUB/ssh" <<'EOS'
#!/usr/bin/env bash
exec bash -s
EOS
cat > "$STUB/docker" <<'EOS'
#!/usr/bin/env bash
case $1 in
  info) exit 0 ;;
  ps) printf 'dumbo-backend\ngotcha30\n' ;;
  inspect) echo "state=running started=x exit=0 restarts=0" ;;
  logs) echo fetch >> "$FAKE_CALLS"
        if [ -n "${FAKE_FAIL:-}" ]; then echo "2026-10-13T00:00:01Z part"; exit 1; fi
        printf '%s\n' "2026-10-13T00:00:01Z INFO start" "2026-10-13T00:00:02Z ERROR boom" \
          "2026-10-13T00:00:03Z WARNING loud" "2026-10-13T00:00:04Z ERROR again" \
          "2026-10-13T00:00:05Z camera_password=hunter2" ;;
esac
EOS
chmod +x "$STUB/ssh" "$STUB/docker"
LOGC="$STUB/logcache"; CALLS="$STUB/calls"; : > "$CALLS"
rl() { PATH="$STUB:$PATH" GOTCHA_SSH=ssh GOTCHA_LOG_CACHE="$LOGC" FAKE_CALLS="$CALLS" \
         "$HERE/remote_logs.sh" axon-gotcha-4 dumbo "$@" 2>&1; }
fetches() { wc -l < "$CALLS"; }
out=$(rl all --grep 'error')
grep -q 'ERROR boom' <<<"$out" && grep -q 'ERROR again' <<<"$out" && ! grep -q WARNING <<<"$out" \
  && [ "$(fetches)" -eq 1 ] && ok "remote_logs: first --grep fetches the log once and filters it" || bad "remote_logs first read: $out"
out=$(rl all --grep 'warning')
grep -q 'WARNING loud' <<<"$out" && grep -q 'from a copy' <<<"$out" && [ "$(fetches)" -eq 1 ] \
  && ok "remote_logs: a second search uses the copy" || bad "remote_logs second read fetched again: $out"
out=$(rl 2)
[ "$(grep -c '^2026' <<<"$out")" -eq 2 ] && grep -q 'ERROR again' <<<"$out" && [ "$(fetches)" -eq 1 ] \
  && ok "remote_logs: a tail is served from a fresh copy" || bad "remote_logs tail: $out"
! grep -rq hunter2 "$LOGC" && ! grep -q hunter2 <<<"$out" && ok "remote_logs: the copy is redacted" || bad "remote_logs copy holds a secret"
[ "$(stat -c %a "$LOGC")" = 700 ] && ok "remote_logs: the copy's folder is private" || bad "remote_logs cache folder mode $(stat -c %a "$LOGC")"
out=$(GOTCHA_LOG_CACHE_TTL=0 rl all --grep 'error')
grep -q 'ERROR boom' <<<"$out" && [ "$(fetches)" -eq 2 ] && ok "remote_logs: TTL=0 reads the site every time" || bad "remote_logs TTL=0: $out"
rm -rf "$LOGC"
out=$(FAKE_FAIL=1 rl all --grep 'part')
grep -q PARTIAL <<<"$out" && grep -q 'part' <<<"$out" && [ ! -e "$LOGC/axon-gotcha-4--dumbo.log" ] \
  && ok "remote_logs: a read cut short says PARTIAL and is not kept" || bad "remote_logs partial: $out"

# --- KB consistency: signatures <-> cases <-> SKILL.md index -----------------------------------
for s in $(grep -vE '^#|^$' "$HERE/signatures.txt" | cut -d'|' -f1 | grep -v '^ok:'); do
  [ -f "$SKILL/kb/cases/$s.md" ] && ok "signature has a case: $s" || bad "signature slug has no case file: $s"
done
for f in "$SKILL"/kb/cases/*.md; do
  b=$(basename "$f" .md)
  grep -q "\`$b\`" "$SKILL/SKILL.md" && ok "indexed: $b" || bad "case not in the SKILL.md index: $b"
done
grep -q 'Platform .\* repositioned' "$HERE/signatures.txt" && bad "signatures.txt tags a routine platform reposition" || ok "no routine-activity signature"
# run_triage.sh sends this file to the site inside a heredoc ending at this marker: a line
# holding it would end the heredoc early, and the rest of the file would run as shell there.
grep -q '__GOTCHA_SIGS__' "$HERE/signatures.txt" && bad "signatures.txt contains the heredoc marker __GOTCHA_SIGS__" || ok "no heredoc marker in signatures.txt"

[ $fail = 0 ] && echo "all checks passed" || { echo "SOME CHECKS FAILED"; exit 1; }
