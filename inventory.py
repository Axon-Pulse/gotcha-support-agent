"""The allowlist of nodes, derived from a gotcha30 config.

This is the ONLY authority for node names and addresses. Tools take node names from
here; the model never supplies a host or an IP.

Credentials are dropped during the walk, not redacted afterwards — you cannot leak a
field you never read.
"""
import ipaddress
import os
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlparse

import yaml

FORBIDDEN = {"password", "passwd", "secret", "token", "api_key", "apikey", "username", "user"}
REPO = Path(os.environ.get("GOTCHA30_REPO", "/home/gal/Documents/code/gotcha30"))
CONFIG = os.environ.get("GOTCHA30_CONFIG", "configs/two_sensor_test.yaml")


def _merge(base: dict, over: dict) -> dict:
    out = dict(base)
    for k, v in over.items():
        out[k] = _merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


def _load(path: Path, seen: frozenset = frozenset()) -> dict:
    if path in seen:
        raise ValueError(f"circular extends at {path}")
    doc = yaml.safe_load(path.read_text()) or {}
    merged: dict = {}
    for parent in doc.get("extends", []) or []:
        merged = _merge(merged, _load(path.parent / parent, seen | {path}))
    return _merge(merged, doc)


def _endpoint(cfg: dict) -> dict | None:
    """Extract host/port from a node config, discarding userinfo and everything else."""
    if not isinstance(cfg, dict):
        return None
    if url := cfg.get("base_url"):
        u = urlparse(str(url))
        return {"host": u.hostname, "port": u.port, "scheme": u.scheme}
    if ip := cfg.get("ip"):
        try:
            ipaddress.ip_address(str(ip))
        except ValueError:
            return None
        return {"host": str(ip), "port": cfg.get("port"), "scheme": "tcp"}
    return None


@lru_cache(maxsize=1)
def load() -> dict[str, dict]:
    """node name -> {type, group, endpoint}. Cached; one config per process."""
    doc = _load(REPO / CONFIG)
    out: dict[str, dict] = {}
    for group, members in (doc.get("nodes") or {}).items():
        if not isinstance(members, dict):
            continue
        for name, spec in members.items():
            if not isinstance(spec, dict) or "type" not in spec:
                continue
            cfg = {k: v for k, v in (spec.get("config") or {}).items()
                   if k.lower() not in FORBIDDEN}
            out[name] = {"name": name, "type": spec["type"],
                         "group": group, "endpoint": _endpoint(cfg)}
    return out


def names() -> list[str]:
    return sorted(load())


def require(name: str) -> dict:
    """Resolve a node name or refuse. The model cannot reach anything not in here."""
    inv = load()
    if name not in inv:
        raise KeyError(f"unknown node {name!r}; known nodes: {sorted(inv)}")
    return inv[name]


def as_prompt() -> str:
    lines = []
    for n in sorted(load().values(), key=lambda r: r["name"]):
        ep = n["endpoint"]
        addr = f" at {ep['scheme']}://{ep['host']}" + (f":{ep['port']}" if ep.get("port") else "") if ep else ""
        lines.append(f"- {n['name']} ({n['type']}, {n['group']}){addr}")
    return "\n".join(lines)
