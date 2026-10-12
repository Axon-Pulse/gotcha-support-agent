"""How to reach each system's machines: address, port, SSH user and SSH password.

Two files at the repo root, both gitignored, in the shape the retired bot used:

  systems_inventory.yaml   systems -> components -> address, port, access.ssh.user/password_env
  secrets.local.env        NAME=password, where NAME is a component's password_env

Addresses, ports and users go to everyone let in. A password leaves this module only through
password(), which server.py calls for an admin with a fresh code, and only for the
password_env a component names: never another entry of the file (the Anthropic key lives
there too), and never the process environment.
"""
import ipaddress
import json
import re
import threading
import time

import yaml

from console.auth import write_private
from console.settings import S

USER_RX = re.compile(r"[A-Za-z_][A-Za-z0-9_-]{0,31}")
HOST_RX = re.compile(r"(?=.{1,253}$)[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?(\.[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*")
ENV_RX = re.compile(r"[A-Z][A-Z0-9_]{0,63}")
MAX_LEN = 4096
SECRETS_HEADER = ("# Device passwords, by the NAME a component's password_env gives in\n"
                  "# systems_inventory.yaml. Edited from the console's Secrets tab. Never commit.\n")

_LOCK = threading.Lock()


class AccessError(ValueError):
    """A change this console won't write. The message is safe to show."""


# --------------------------------------------------------------------------
# Reading
# --------------------------------------------------------------------------

def _doc() -> dict:
    try:
        d = yaml.safe_load(S.inventory_file.read_text(encoding="utf-8")) or {}
    except FileNotFoundError:
        return {}
    return d if isinstance(d, dict) else {}


def _secrets() -> dict[str, str]:
    """NAME -> value, read the way the retired bot's secrets_store reads it."""
    out = {}
    try:
        text = S.device_secrets_file.read_text(encoding="utf-8")
    except FileNotFoundError:
        return out
    for line in text.splitlines():
        if not line.strip() or line.lstrip().startswith("#") or "=" not in line:
            continue
        name, _, value = line.partition("=")
        if ENV_RX.fullmatch(name.strip()):
            out[name.strip()] = value
    return out


def _meta() -> dict:
    try:
        return json.loads(S.access_meta_file.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        return {}


def _ssh(comp: dict) -> dict:
    a = comp.get("access")
    s = a.get("ssh") if isinstance(a, dict) else None
    return s if isinstance(s, dict) else {}


def listing() -> dict:
    doc, secrets, meta = _doc(), _secrets(), _meta()
    systems = []
    for sname, sysd in (doc.get("systems") or {}).items():
        sysd = sysd if isinstance(sysd, dict) else {}
        comps = []
        for cname, c in (sysd.get("components") or {}).items():
            c = c if isinstance(c, dict) else {}
            ssh = _ssh(c)
            env = ssh.get("password_env")
            m = meta.get(f"{sname}/{cname}", {})
            comps.append({"name": str(cname), "type": c.get("type"), "role": c.get("role"),
                          "address": c.get("address"), "port": c.get("port"),
                          "scheme": c.get("scheme"), "user": ssh.get("user"),
                          "has_password": bool(env and secrets.get(env)),
                          "updated_at": m.get("at"), "updated_by": m.get("by")})
        systems.append({"name": str(sname), "site": sysd.get("site"),
                        "description": sysd.get("description"), "components": comps})
    return {"systems": systems, "inventory_file": S.inventory_file.exists()}


def _find(doc: dict, system: str, component: str) -> dict:
    for sname, sysd in (doc.get("systems") or {}).items():
        if str(sname) == system and isinstance(sysd, dict):
            for cname, c in (sysd.get("components") or {}).items():
                if str(cname) == component and isinstance(c, dict):
                    return c
    raise KeyError(f"{system}/{component}")


def password(system: str, component: str) -> str | None:
    env = _ssh(_find(_doc(), system, component)).get("password_env")
    return _secrets().get(env) if env else None


# --------------------------------------------------------------------------
# Writing
# --------------------------------------------------------------------------

def _check_address(v: str) -> str:
    v = v.strip()
    try:
        return str(ipaddress.ip_address(v))
    except ValueError:
        if HOST_RX.fullmatch(v):
            return v
    raise AccessError(f"{v!r} is not an IP address or host name")


def _env_name(system: str, component: str, taken: set[str]) -> str:
    base = re.sub(r"[^A-Z0-9]+", "_", f"{system}_{component}".upper()).strip("_")
    name = f"{base}_SSH_PW" if base[:1].isalpha() else f"SYS_{base}_SSH_PW"
    n, out = 2, name
    while out in taken:
        out, n = f"{name}_{n}", n + 1
    return out


def update(system: str, component: str, who: str, *, address: str | None = None,
           port: int | None = None, user: str | None = None, password: str | None = None,
           clear_password: bool = False) -> list[str]:
    """Change what was given, leave the rest. Returns the names of the fields changed."""
    if password is not None:
        if not password:
            raise AccessError("the password is empty; use clear to remove it")
        if len(password) > MAX_LEN or any(c in password for c in "\n\r\0"):
            raise AccessError("the password has a line break in it, or is too long")
    if port is not None and not (1 <= port <= 65535):
        raise AccessError("the port is a number from 1 to 65535")
    if user is not None and user and not USER_RX.fullmatch(user):
        raise AccessError("an SSH user is letters, digits, _ and -, starting with a letter or _")
    with _LOCK:
        doc = _doc()
        try:
            comp = _find(doc, system, component)
        except KeyError:
            raise AccessError(f"no component {component} in system {system}")
        changed = []
        if address is not None and address.strip() != str(comp.get("address") or ""):
            comp["address"] = _check_address(address)
            changed.append("address")
        if port is not None and port != comp.get("port"):
            comp["port"] = port
            changed.append("port")
        ssh = comp.setdefault("access", {}).setdefault("ssh", {}) \
            if (user or password) else _ssh(comp)
        if user is not None and user != (ssh.get("user") or ""):
            if user:
                ssh["user"] = user
            else:
                ssh.pop("user", None)
            changed.append("user")
        secrets = _secrets()
        if password is not None or clear_password:
            env = ssh.get("password_env")
            if not env and password is not None:
                env = _env_name(system, component, set(secrets))
                ssh["password_env"] = env
                changed.append("password_env")
            if env:
                if password is not None:
                    secrets[env] = password
                else:
                    secrets.pop(env, None)
                changed.append("password")
        if not changed:
            return []
        if "password" in changed:
            write_private(S.device_secrets_file,
                          SECRETS_HEADER + "".join(f"{k}={v}\n" for k, v in sorted(secrets.items())))
        if set(changed) - {"password"}:
            write_private(S.inventory_file, yaml.safe_dump(doc, sort_keys=False, allow_unicode=True))
        meta = _meta()
        meta[f"{system}/{component}"] = {"at": round(time.time(), 3), "by": who}
        write_private(S.access_meta_file, json.dumps(meta, indent=1, sort_keys=True))
    return changed
