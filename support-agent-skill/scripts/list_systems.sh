#!/usr/bin/env bash
# List tailnet peers, optionally narrowed to ones matching what the PM called the system.
#
#   list_systems.sh [words the PM used, e.g. "gotcha 3"]
#
# Matching ignores case and punctuation, so "Gotcha 3", "gotcha-3" and "gotcha3" all match
# "axon-gotcha-3". With no match it prints every peer so the PM can pick.
set -uo pipefail
command -v tailscale >/dev/null || { echo "tailscale is not installed on this machine" >&2; exit 3; }

tailscale status --json 2>/dev/null | python3 -c '
import json, re, sys
q = re.sub(r"[^a-z0-9]", "", " ".join(sys.argv[1:]).lower())
st = json.load(sys.stdin)
peers = list((st.get("Peer") or {}).values())
rows = []
for p in peers:
    host = p.get("HostName", "")
    dns = (p.get("DNSName") or "").rstrip(".").split(".")[0]
    ip = (p.get("TailscaleIPs") or [""])[0]
    rows.append((dns or host, host, ip, "online" if p.get("Online") else "offline",
                 "tailscale" if p.get("sshHostKeys") else "ssh", (p.get("LastSeen") or "")[:16]))
norm = lambda s: re.sub(r"[^a-z0-9]", "", s.lower())
hits = [r for r in rows if q and (q in norm(r[0]) or q in norm(r[1]))]
if q and not hits:
    print(f"no peer matches {sys.argv[1:]!r}; all peers:")
shown = hits or rows
shown.sort(key=lambda r: (r[3] != "online", r[0]))
fmt = "%-28s %-24s %-16s %-8s %-8s %s"
print(fmt % ("tailnet name", "hostname", "ip", "state", "via", "last seen"))
for r in shown:
    print(fmt % (r[0], r[1], r[2], r[3], r[4], r[5] if r[3] == "offline" else ""))
' "$@"
