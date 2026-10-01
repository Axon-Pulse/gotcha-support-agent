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
for args in "h" "h 'a;b'" "h dumbo 0" "h dumbo 501" "h dumbo 10 -f" "h dumbo --grep" "h dumbo 5 x"; do
  eval "\"$HERE/remote_logs.sh\" $args" >/dev/null 2>&1; [ $? -eq 2 ] && ok "remote_logs refuses: $args" || bad "remote_logs accepted: $args"
done

# --- KB consistency: signatures <-> cases <-> SKILL.md index -----------------------------------
for s in $(grep -vE '^#|^$' "$HERE/signatures.txt" | cut -d'|' -f1 | grep -v '^ok:'); do
  [ -f "$SKILL/kb/cases/$s.md" ] && ok "signature has a case: $s" || bad "signature slug has no case file: $s"
done
for f in "$SKILL"/kb/cases/*.md; do
  b=$(basename "$f" .md)
  grep -q "\`$b\`" "$SKILL/SKILL.md" && ok "indexed: $b" || bad "case not in the SKILL.md index: $b"
done
grep -q 'Platform .\* repositioned' "$HERE/signatures.txt" && bad "signatures.txt tags a routine platform reposition" || ok "no routine-activity signature"

[ $fail = 0 ] && echo "all checks passed" || { echo "SOME CHECKS FAILED"; exit 1; }
