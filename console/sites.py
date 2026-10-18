"""A site's sensors, from its newest saved triage, and running a new triage on demand.

run_triage.sh saves each run as <host>-<YYYYmmdd-HHMMSS>.txt. The bot's are in
slack-bridge/state/triage/ (its /tmp/gotcha-triage); one run by hand on this host goes to
/tmp/gotcha-triage. Three sections of it describe the sensors:

- "site config: sensor addresses": the site's own config, grepped for platforms (`  /name:`),
  nodes (`    name:`), their `type:` and their address keys (ip, base_url, host, address).
- "sensor paths": each of those addresses as this machine reaches it (route, neighbour
  cache, ping), plus the radar behind each magos APU, which is derived, not configured.
- "sensor node status": each sensor node's newest status line from the core log.

Nothing here connects to a site except run_triage(), which runs the skill's own read-only
script, as the bot does for a Slack question.
"""
import os
import re
import subprocess
import time
from datetime import datetime
from pathlib import Path

from console.access import SENSOR_CATEGORIES
from console.settings import S

_NAME = re.compile(r"^(.+)-(\d{8}-\d{6})\.txt$")
_SECTION = re.compile(r"^=== (.*) ===$")
_ADDR_KEYS = ("ip", "base_url", "host", "address")


def triage_files(host: str) -> list[Path]:
    """The host's saved triages, newest first, from every triage directory."""
    out = []
    for d in S.triage_dirs:
        for p in d.glob(f"{host}-*.txt"):
            m = _NAME.match(p.name)
            if m and m.group(1) == host:
                out.append(p)
    return sorted(out, key=lambda p: _NAME.match(p.name).group(2), reverse=True)


def _when(p: Path) -> float:
    try:
        return datetime.strptime(_NAME.match(p.name).group(2), "%Y%m%d-%H%M%S").timestamp()
    except ValueError:
        return p.stat().st_mtime


def sections(text: str) -> dict[str, str]:
    out, title, buf = {}, None, []
    for line in text.split("\n"):
        m = _SECTION.match(line)
        if m:
            if title is not None:
                out[title] = "\n".join(buf).strip("\n")
            title, buf = m.group(1), []
        elif title is not None:
            buf.append(line)
    if title is not None:
        out[title] = "\n".join(buf).strip("\n")
    return out


def _section(secs: dict, prefix: str) -> tuple[str, str] | tuple[None, None]:
    for t, body in secs.items():
        if t.startswith(prefix):
            return t, body
    return None, None


def _value(raw: str) -> tuple[str, str]:
    """'"192.168.44.60"      # TODO: confirm' -> ('192.168.44.60', 'TODO: confirm')."""
    v, _, note = raw.partition(" #")
    return v.strip().strip("\"'"), note.strip()


def parse_site_config(body: str) -> dict:
    """{file, groups: [{platform, nodes: [{name, type, addresses: [{key, value, note}]}]}], notes}"""
    out = {"file": None, "groups": [], "notes": []}
    group = node = None
    for line in body.split("\n"):
        if line.startswith("file: "):
            out["file"] = line[6:].strip()
            continue
        if line.startswith("NOTE:") or line.startswith("site config not readable"):
            out["notes"].append(line)
            continue
        m = re.match(r"^\d+:(.*)$", line)
        if not m:
            continue
        content = m.group(1)
        if re.match(r"^  /\S+:\s*$", content):
            group = {"platform": content.strip()[:-1], "nodes": []}
            out["groups"].append(group)
            node = None
        elif re.match(r"^    [A-Za-z0-9_-]+:\s*$", content):
            if group is None:
                group = {"platform": "", "nodes": []}
                out["groups"].append(group)
            node = {"name": content.strip()[:-1], "type": "", "addresses": []}
            group["nodes"].append(node)
        elif node is not None:
            k = re.match(r"^\s*([A-Za-z_]+):\s*(.*)$", content)
            if not k:
                continue
            key, (value, note) = k.group(1), _value(k.group(2))
            if key == "type" and not node["type"]:
                node["type"] = value
            elif key in _ADDR_KEYS and value:
                node["addresses"].append({"key": key, "value": value, "note": note})
    return out


def parse_paths(body: str) -> list[dict]:
    """One line per address: `ip  dev X src Y  neigh=STATE  [ping OK|DOWN]  [flag]  [<- label]`."""
    out = []
    for line in body.split("\n"):
        m = re.match(r"^(\d+\.\d+\.\d+\.\d+)\s+(.*)$", line)
        if not m:
            continue
        rest = m.group(2)
        neigh = re.search(r"neigh=(\S+)", rest)
        ping = re.search(r"ping (OK|DOWN|not run)", rest)
        label = rest.split("<- ", 1)[1].strip() if "<- " in rest else ""
        out.append({"ip": m.group(1),
                    "route": rest.split("neigh=")[0].strip(),
                    "neigh": neigh.group(1) if neigh else "",
                    "ping": ping.group(1) if ping else "",
                    "hijack": "ROUTED VIA TAILNET" in rest,
                    "label": label})
    return out


def parse_status(body: str) -> dict[str, str]:
    """node -> its newest status line (`magos0: CONNECTED | ... radar=stopped`)."""
    out = {}
    for line in body.split("\n"):
        m = re.search(r"\b([A-Za-z0-9_]+): +([A-Z]+) +\|", line)
        if m:
            out[m.group(1)] = line[m.start():].strip()
    return out


def _ip_of(value: str) -> str | None:
    m = re.search(r"\b(\d+\.\d+\.\d+\.\d+)\b", value)
    return m.group(1) if m else None


def site(host: str) -> dict:
    """The newest triage of the host, read into platforms and sensors."""
    files = triage_files(host)
    if not files:
        return {"host": host, "triage": None}
    p = files[0]
    secs = sections(p.read_text(encoding="utf-8", errors="replace"))
    t_cfg, cfg_body = _section(secs, "site config: sensor addresses")
    _, paths_body = _section(secs, "sensor paths")
    _, status_body = _section(secs, "sensor node status")
    cfg = parse_site_config(cfg_body or "")
    paths = parse_paths(paths_body or "")
    by_ip = {x["ip"]: x for x in paths}
    status = parse_status(status_body or "")
    used = set()
    for g in cfg["groups"]:
        for n in g["nodes"]:
            n["status"] = status.get(n["name"])
            for a in n["addresses"]:
                ip = _ip_of(a["value"])
                a["path"] = by_ip.get(ip) if ip else None
                if ip in by_ip:
                    used.add(ip)
    unreachable = "UNREACHABLE" in (secs.get("host") or "") or not secs
    return {"host": host, "triage": {
        "file": str(p), "ts": _when(p), "count": len(files),
        "config_section": t_cfg, "config": cfg,
        # Addresses the triage checked that no config node lists: the radar behind each
        # magos APU (derived), and addresses found only in the resolved config.
        "other_paths": [x for x in paths if x["ip"] not in used],
        "status_found": bool(status), "unreachable": unreachable,
        "raw": {k: secs.get(k) or "" for k in secs if k.startswith(("site config", "sensor"))}}}


# --------------------------------------------------------------------------
# Running a triage
# --------------------------------------------------------------------------

_running: dict[str, float] = {}


def run_triage(host: str, ssh_user: str | None, allowed: str, timeout_s: int = 120) -> dict:
    """run_triage.sh <host>, read-only, saving where the bot saves. One run per host at a time."""
    if not re.fullmatch(allowed, host):
        raise PermissionError(f"{host} is not one of the bot's sites ({allowed})")
    if _running.get(host, 0) > time.time() - timeout_s:
        raise RuntimeError(f"a triage of {host} is already running")
    _running[host] = time.time()
    out_dir = S.triage_dirs[0]
    out_dir.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, "GOTCHA_TRIAGE_DIR": str(out_dir), "GOTCHA_SYSTEMS_REGEX": allowed,
           "GOTCHA_TRIAGE_TIMEOUT": str(timeout_s - 10)}
    if ssh_user:
        env["GOTCHA_SSH_USER"] = ssh_user
    try:
        r = subprocess.run([str(S.skill_dir / "scripts" / "run_triage.sh"), host], env=env,
                           capture_output=True, text=True, timeout=timeout_s)
        ok, tail = r.returncode == 0, (r.stdout + r.stderr)[-1500:]
    except subprocess.TimeoutExpired:
        ok, tail = False, f"the triage did not finish in {timeout_s}s"
    except OSError as e:
        ok, tail = False, f"could not run run_triage.sh: {e}"
    finally:
        _running.pop(host, None)
    return {"ok": ok, "output_tail": tail, "site": site(host)}


# --------------------------------------------------------------------------
# System access against the site's own config
# --------------------------------------------------------------------------

def config_tail(path: str) -> str:
    """A config path from where the site's configs start: the launcher's /app/configs/x.yaml,
    the host's /home/u/gotcha-…/configs/x.yaml and configs/x.yaml are the same file."""
    i = path.find("configs/")
    return path[i:] if i >= 0 else path.lstrip("./")


def _addr_of(value: str) -> str:
    """What to compare an address by: its IP when it has one, else the host name."""
    v = value.strip().lower()
    return _ip_of(v) or re.sub(r"^\w+://|[:/].*$", "", v)


def _local(addr: str) -> bool:
    """An address on the machine itself: a node's own service, not a component."""
    return addr in ("localhost", "0.0.0.0") or addr.startswith("127.")  # noqa: S104


def compare(system: dict, site: dict) -> dict:
    """The system's components (as System access lists them, no secrets) against the newest
    triage's reading of the site config. Two lists: sensors whose address the config doesn't
    have, and config nodes with an address no component has."""
    t = site.get("triage")
    if not t or t["unreachable"] or not t["config"]["groups"]:
        raise ValueError("the site's config couldn't be read; run a triage when the site is online")
    config = [{"platform": g["platform"], "node": n["name"], "type": n["type"],
               "address": a["value"], "addr": _addr_of(a["value"])}
              for g in t["config"]["groups"] for n in g["nodes"] for a in n["addresses"]]
    config = [c for c in config if not _local(c["addr"])]
    # The radar behind each magos APU isn't in the config, but the triage derives it: a sensor
    # at that address is where the config says it is.
    known = {c["addr"] for c in config} | {p["ip"] for p in t["other_paths"] if p["label"]}
    comps = [(cat["key"], c) for cat in system["categories"] for c in cat["components"]]
    ours = {_addr_of(str(c[k])) for _, c in comps for k in ("address", "computer_ip", "rustdesk_ip") if c.get(k)}
    return {
        "triage_ts": t["ts"], "config_file": t["config"]["file"],
        "not_in_config": [{"component": c["name"], "category": cat, "address": c["address"]}
                          for cat, c in comps if cat in SENSOR_CATEGORIES and c.get("address")
                          and _addr_of(str(c["address"])) not in known],
        "unlisted": [{k: c[k] for k in ("platform", "node", "type", "address")}
                     for c in config if c["addr"] not in ours],
        "checked": sum(1 for cat, c in comps if cat in SENSOR_CATEGORIES and c.get("address")),
    }
