"""Can the agent actually see each system? One command, before anything goes live.

    python run.py --preflight [--system gotcha3]

Runs LIVE whatever AGENT_MODE says — its whole point is the real machines — but only
read-only commands: `true`, `command -v`, `docker ps`, the allowlisted health capture,
and the config read. Every line is ok / warn / FAIL with the reason and what to do.
"""
from __future__ import annotations

import shutil
import subprocess

import inventory
import transport

OK, WARN, FAIL = "ok  ", "warn", "FAIL"
_TOOLS = ("ecal_mon_cli", "docker", "curl", "ping", "ip", "timeout")


def _sh(host: str, remote: str, timeout: int = 20) -> subprocess.CompletedProcess:
    """A fixed, preflight-owned command on a system's host. Never model input."""
    prefix, env, ssh, _ = transport._login(host)
    return subprocess.run(["timeout", str(timeout + 5), *prefix, *ssh, "--", remote],
                          capture_output=True, text=True, timeout=timeout + 10, env=env)


def _known_host(ip: str, port: int) -> bool:
    key = ip if port == 22 else f"[{ip}]:{port}"
    r = subprocess.run(["ssh-keygen", "-F", key], capture_output=True, text=True)
    return r.returncode == 0 and bool(r.stdout.strip())


def check_system(name: str) -> list[tuple[str, str, str]]:
    out: list[tuple[str, str, str]] = []
    add = lambda st, what, detail="": out.append((st, what, detail))  # noqa: E731
    spec = inventory.systems()[name]
    host = spec.get("host")
    if not host:
        add(FAIL if inventory.system_required() else WARN, "host",
            "no host set — pick the component gotcha runs on in the inventory")
        return out
    rec = inventory.load()[host]
    ip = (rec.get("endpoint") or {}).get("host")
    try:
        creds = transport.login_info(host)
    except KeyError as e:
        add(FAIL, "ssh access", f"{e} — add user and key_file for {host!r}")
        return out
    add(OK, "host", f"{host} at {ip}, as {creds['user']}")
    if not creds["has_password"] and creds.get("password_env"):
        add(FAIL, "ssh password", f"{creds['password_env']} is not set in the environment "
                                  f"or the console's secret store")
        return out
    if creds["has_password"] and not shutil.which("sshpass"):
        add(FAIL, "sshpass", "password login needs sshpass on this server "
                             "(sudo apt install sshpass) — or switch this host to a key")
        return out
    if not _known_host(ip, creds["port"]):
        add(WARN, "host key", f"{ip} is not in known_hosts: the first connection will trust "
                              f"whatever answers. Connect once by hand and check the "
                              f"fingerprint: ssh -p {creds['port']} {creds['user']}@{ip}")

    r = _sh(host, "true")
    if r.returncode != 0:
        add(FAIL, "ssh login", (r.stderr or f"exit {r.returncode}").strip()[-200:])
        return out
    add(OK, "ssh login")

    r = _sh(host, "for t in " + " ".join(_TOOLS)
            + "; do command -v $t >/dev/null || echo $t; done")
    missing = r.stdout.split()
    add(FAIL if missing else OK, "tools on host",
        f"missing: {', '.join(missing)}" if missing else ", ".join(_TOOLS))

    r = _sh(host, "docker ps -a --format '{{.Names}}' >/dev/null")
    add(OK if r.returncode == 0 else FAIL, "docker readable",
        "" if r.returncode == 0 else
        f"{creds['user']} cannot run docker ps — add it to the docker group "
        f"({(r.stderr or '').strip()[-120:]})")

    with inventory.using(name):
        if spec.get("config"):
            nodes, err = inventory.config_nodes(name)
            add(FAIL if err else OK, "config", err or f"{spec['config']}: {len(nodes)} nodes")
        else:
            add(WARN, "config", "no config path set — only inventory components are known")

        try:
            from tools.health import get_system_health
            h = get_system_health()
            seen = {row.get("node") for row in h.get("nodes") or [] if row.get("node")}
            if not seen:
                add(FAIL, "eCAL health", "no /system/health messages — is gotcha running, "
                                         "and is ecal_mon_cli on the same network?")
            else:
                unknown = sorted(seen - set(inventory.view()))
                add(WARN if unknown else OK, "eCAL health",
                    f"{len(seen)} nodes publishing"
                    + (f"; not in inventory or config: {', '.join(unknown)} — the agent "
                       f"cannot route to or probe these" if unknown else ""))
        except Exception as e:                     # noqa: BLE001 - reported
            add(FAIL, "eCAL health", f"{type(e).__name__}: {e}")

        for comp, crec in sorted(inventory.view().items()):
            if comp == host or crec.get("system") != name:
                continue
            if comp not in inventory._access():
                continue                           # no access block: nothing to log into
            try:
                txt = transport.run_on("remote_uptime", comp).strip()
                add(OK if txt else FAIL, f"unit {comp}",
                    "reached through " + host if txt else "no answer through " + host)
            except Exception as e:                 # noqa: BLE001
                add(FAIL, f"unit {comp}", f"{type(e).__name__}: {e}")
    return out


def main(only: str | None = None) -> int:
    was, transport.MODE = transport.MODE, "live"   # the point is the real machines
    try:
        return _main(only)
    finally:
        transport.MODE = was


def _main(only: str | None) -> int:
    try:
        known = inventory.systems()
    except inventory.InventoryError as e:
        print(f"FAIL inventory: {e}")
        return 1
    if not shutil.which("ssh"):
        print("FAIL this server has no ssh client")
        return 1
    names = [only] if only else sorted(known)
    if only and only not in known:
        print(f"FAIL no system {only!r}; known: {', '.join(sorted(known))}")
        return 1
    failed = False
    for n in names:
        print(f"\n== {n}" + (f"  (site {known[n]['site']})" if known[n].get("site") else ""))
        for st, what, detail in check_system(n):
            failed |= st == FAIL
            print(f"  {st}  {what}" + (f" — {detail}" if detail else ""))
    checked = [n for n in names if known[n].get("host")]
    print("\n" + ("Some checks FAILED — fix those before going live." if failed
                  else "No system has a host yet, so nothing was checked over SSH." if not checked
                  else f"All {len(checked)} system(s) with a host are reachable."))
    return 1 if failed else 0
