"""The allowlist of systems, and the one place credentials are resolved.

Two views of the same registry, and the whole design is keeping them apart:

  PUBLIC  — load(), names(), require(), as_prompt(). Logical name, type, domain, site,
            description, endpoint. This is what reaches the model, the tool enums and
            the LangGraph state. It is built with a field ALLOWLIST, so a key nobody
            anticipated cannot ride along into a prompt.

  PRIVATE — credentials(). Resolved at call time, never cached, never returned into
            graph state. Values come back wrapped in Secret, which renders as "***"
            through str(), repr(), f-strings and json.dumps(default=str) — every path
            by which a credential would otherwise reach a log line or a tool result.

Two sources feed the public view: the gotcha30 deployment config (which nodes the
launcher runs) and, if present, systems_inventory.yaml (the estate across sites, with
access details). Neither may contain a secret VALUE — see _reject_inline_secrets.

This is the ONLY authority for names and addresses. The model never supplies a host;
it supplies a logical name that must resolve here, or the call is refused.
"""
import ipaddress
import os
import re
import time
from contextlib import contextmanager
from contextvars import ContextVar
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlparse

import yaml

import secrets_store

ROOT = Path(__file__).resolve().parent
REPO = Path(os.environ.get("GOTCHA30_REPO", "/home/gal/Documents/code/gotcha30"))
CONFIG = os.environ.get("GOTCHA30_CONFIG", "configs/two_sensor_test.yaml")
SYSTEMS = Path(os.environ.get("SYSTEMS_INVENTORY", ROOT / "systems_inventory.yaml"))

FORBIDDEN = {"password", "passwd", "secret", "token", "api_key", "apikey", "username", "user"}

# Keys that would hold a secret VALUE. Their presence anywhere in the systems file is a
# hard load error: a secret in a file that gets read into a model-facing process is a
# problem no amount of later redaction fixes. Matched exactly, so `key_file` and
# `password_env` — which hold a path and an env var NAME — stay legal.
_INLINE_SECRET_KEYS = {
    "password", "passwd", "pass", "secret", "token", "api_key", "apikey", "auth",
    "ssh_password", "credential", "credentials", "private_key", "key",
}
# Everything the model may see about a system. An allowlist, not a denylist: a key
# nobody anticipated cannot ride along into a prompt by being added to the YAML.
_PUBLIC_FIELDS = ("name", "type", "role", "domain", "group", "system", "description",
                  "hardware", "software_version", "web", "endpoint", "location",
                  # Operator-defined per-type fields (az, elevation, …). Allowlisted as
                  # ONE key rather than by widening the list per field: the guarantee is
                  # that an unanticipated key cannot ride along, and a single namespaced
                  # dict keeps that true while still letting the registry grow. Secret
                  # detection still runs inside it — see _reject_inline_secrets.
                  "fields")

# A system holds more than sensors: the compute box that runs the stack, the switch the
# sensors hang off, the laptop an engineer left on site. They are addressable and worth
# diagnosing, so they live in the same namespace; `role` is what tells them apart.
ROLES = ("sensor", "compute", "laptop", "network", "power", "other")
DEFAULT_ROLE = "sensor"

# gotcha30 spells the camera family "optic". The console and the runbook call it
# "camera". Normalised once, at load, so there are never two domain keys meaning the
# same hardware — one of which would match no agent.
DOMAIN_ALIASES = {"optic": "camera", "optics": "camera", "eo": "camera", "ir": "camera"}
_ACCESS_FIELDS = {"user", "port", "key_file", "password_env"}
_USER_RX = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]{0,31}$")


class InventoryError(ValueError):
    """A registry that cannot be loaded safely. The message names the key to fix."""


class Secret:
    """A credential value that cannot be printed, logged or serialised by accident.

    Every stringification path redacts. `reveal()` is the single deliberate way out,
    and it is called only inside transport.py, where the value goes into a child
    process environment rather than an argv or a log line.
    """
    __slots__ = ("_value", "source")

    def __init__(self, value: str, source: str):
        self._value = value
        self.source = source

    def reveal(self) -> str:
        return self._value

    def __str__(self) -> str:            # str(), print(), "%s", json.dumps(default=str)
        return "***"

    def __repr__(self) -> str:           # repr(), log("%r"), pytest assertion output
        return f"<Secret from {self.source}>"

    def __format__(self, spec: str) -> str:   # f"{secret}"
        return "***"

    def __bool__(self) -> bool:
        return bool(self._value)


# ---------------------------------------------------------------------------
# gotcha30 deployment config
# ---------------------------------------------------------------------------

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


def _gotcha30_nodes() -> dict[str, dict]:
    """Nodes the launcher runs. Credentials are dropped during the walk, not after.

    An absent deployment config is a normal state, not a crash: a fresh checkout, a
    different machine, or an operator who renamed the config they were running. It used
    to raise FileNotFoundError out of a module-level `@tool` decorator, which took down
    every import of tools/ — console, tests and agents alike — for a missing optional
    file. Degrade to "no nodes known from config" instead; the systems file still
    overlays on top, so an explicitly inventoried system stays addressable.
    """
    path = REPO / CONFIG
    if not path.exists():
        return {}
    return _nodes_from_config(_load(path))


def _nodes_from_config(doc: dict) -> dict[str, dict]:
    """A gotcha30 config's node list, secrets dropped — local file or one read over SSH."""
    out: dict[str, dict] = {}
    for group, members in (doc.get("nodes") or {}).items():
        if not isinstance(members, dict):
            continue
        for name, spec in members.items():
            if not isinstance(spec, dict) or "type" not in spec:
                continue
            cfg = {k: v for k, v in (spec.get("config") or {}).items()
                   if k.lower() not in FORBIDDEN}
            out[name] = {"name": name, "type": spec["type"], "group": group,
                         "domain": None, "description": None, "endpoint": _endpoint(cfg)}
    return out


# ---------------------------------------------------------------------------
# systems_inventory.yaml
# ---------------------------------------------------------------------------

def _reject_inline_secrets(node, path: str = "") -> None:
    """Refuse to load a registry holding a secret value. Fail loud, fail at startup."""
    if isinstance(node, dict):
        for k, v in node.items():
            where = f"{path}.{k}" if path else str(k)
            if str(k).lower() in _INLINE_SECRET_KEYS:
                raise InventoryError(
                    f"{SYSTEMS.name} contains an inline secret at {where!r}. This file must "
                    f"never hold a credential value. Use 'password_env: NAME_OF_ENV_VAR' or "
                    f"'key_file: /path/to/key' instead, and put the secret in the environment.")
            _reject_inline_secrets(v, where)
    elif isinstance(node, list):
        for i, v in enumerate(node):
            _reject_inline_secrets(v, f"{path}[{i}]")


def _system_endpoint(spec: dict) -> dict | None:
    addr = spec.get("address")
    if not addr:
        return None
    host = str(addr)
    try:
        ipaddress.ip_address(host)
    except ValueError:
        if host != "localhost":
            raise InventoryError(
                f"address {host!r} is not an IP literal; give an address, not a hostname")
    return {"host": host, "port": spec.get("port"), "scheme": spec.get("scheme", "tcp")}


@lru_cache(maxsize=1)
def _systems_doc() -> dict:
    if not SYSTEMS.exists():
        return {}
    doc = yaml.safe_load(SYSTEMS.read_text()) or {}
    _reject_inline_secrets(doc)          # before anything else touches it
    return doc


def _iter_components(doc: dict):
    """Yield (name, spec, system name, system spec) over every shape.

    A system groups components — sensors, but also the compute box, the switch, the
    laptop:

        systems: {tower1: {site: .., components: {magos: {type: .., role: sensor}}}}

    `sensors:` is still read as an alias for `components:`, so registries written before
    the model widened keep loading and keep defaulting to role=sensor. A system may also
    be a single component itself, which is the flat shape:

        systems: {magos: {type: .., address: ..}}

    Logical names live in ONE namespace regardless of nesting or role — that is what a
    tool argument resolves against — so a name used twice is a load error, not a silent
    last-one-wins.
    """
    seen: dict[str, str] = {}
    for sys_name, sys_spec in (doc.get("systems") or {}).items():
        if not isinstance(sys_spec, dict):
            continue
        members = sys_spec.get("components")
        if not isinstance(members, dict):
            members = sys_spec.get("sensors")
        pairs = (members.items() if isinstance(members, dict)
                 else [(sys_name, sys_spec)] if "type" in sys_spec else [])
        for name, spec in pairs:
            if not isinstance(spec, dict) or "type" not in spec:
                continue
            if name in seen:
                raise InventoryError(
                    f"component name {name!r} is defined twice — in {seen[name]!r} and "
                    f"{sys_name!r}. Logical names must be unique across all systems, "
                    f"because a tool argument resolves against this one namespace.")
            role = str(spec.get("role") or DEFAULT_ROLE)
            if role not in ROLES:
                raise InventoryError(
                    f"{name!r} has role {role!r}; allowed roles are {list(ROLES)}")
            seen[name] = sys_name
            yield name, spec, sys_name, sys_spec


def _systems() -> dict[str, dict]:
    """Public records from the central registry. The access block is not carried over."""
    out: dict[str, dict] = {}
    for name, spec, sys_name, sys_spec in _iter_components(_systems_doc()):
        declared = str(spec.get("domain") or "").strip().lower()
        out[name] = {
            "name": name, "type": spec["type"],
            "role": str(spec.get("role") or DEFAULT_ROLE),
            "domain": DOMAIN_ALIASES.get(declared, declared) or None,
            "group": spec.get("site") or sys_spec.get("site") or sys_name,
            "system": sys_name,
            "description": spec.get("description") or sys_spec.get("description"),
            "hardware": spec.get("hardware"),
            "software_version": spec.get("software_version"),
            # A device's own web UI. Not a secret, and useful to say "the dashboard is
            # on :5173" — so it is on the allowlist deliberately, not by omission.
            "web": spec.get("web"),
            # Where the thing physically is. Inherited from the system unless the
            # component overrides it — sensors on one mast share a location, and
            # repeating it on each is how the two drift apart.
            "location": _location(spec) or _location(sys_spec),
            "fields": {k: v for k, v in (spec.get("fields") or {}).items()
                       if isinstance(spec.get("fields"), dict)},
            "endpoint": _system_endpoint(spec)}
    return out


def _location(spec: dict) -> dict | None:
    """lat/lon as a pair, or nothing. Half a coordinate is not a location."""
    if not isinstance(spec, dict):
        return None
    lat, lon = spec.get("lat"), spec.get("lon")
    try:
        lat, lon = float(lat), float(lon)
    except (TypeError, ValueError):
        return None
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return None
    return {"lat": lat, "lon": lon}


@lru_cache(maxsize=1)
def _access() -> dict[str, dict]:
    """How to reach each system: user, port, key path, env var NAME. No secret values.

    Safe to cache precisely because it holds no secrets — only the coordinates for
    resolving one later.
    """
    doc = _systems_doc()
    default = ((doc.get("defaults") or {}).get("ssh") or {})
    out: dict[str, dict] = {}
    for name, spec, _sys_name, sys_spec in _iter_components(doc):
        # A system may set access once for every sensor it holds; a sensor may override.
        inherited = ((sys_spec.get("access") or {}).get("ssh") or {}) if sys_spec is not spec else {}
        own = ((spec or {}).get("access") or {}).get("ssh")
        if own is None and not inherited:
            continue
        ssh = {**inherited, **(own or {})}
        if unknown := set(ssh) - _ACCESS_FIELDS:
            raise InventoryError(f"{name}: unknown access.ssh keys {sorted(unknown)}; "
                                 f"allowed: {sorted(_ACCESS_FIELDS)}")
        merged = {**default, **ssh}
        user = str(merged.get("user", ""))
        if not _USER_RX.match(user):
            raise InventoryError(f"{name}: access.ssh.user {user!r} is not a valid username")
        port = int(merged.get("port", 22))
        if not 1 <= port <= 65535:
            raise InventoryError(f"{name}: access.ssh.port {port} out of range")
        out[name] = {"user": user, "port": port, "key_file": merged.get("key_file"),
                     "password_env": merged.get("password_env")}
    return out


# ---------------------------------------------------------------------------
# public view
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# systems, and the one a session is about
# ---------------------------------------------------------------------------
#
# In a central deployment every system runs gotcha on its OWN machine, and this server
# reaches that machine over SSH. A system therefore names two things:
#
#     host:   the component gotcha runs on — where every "local" check actually runs
#     config: that system's gotcha config, as a path ON THE HOST
#
# A session is about exactly one system. While it is active (using()), the view below
# is that system and nothing else: its components, plus the nodes its own config
# declares. A tool cannot address another site's hardware, because it is not there.

# Letters, digits and _ . / - only, optionally from ~/. It is sent to a remote shell,
# so nothing that shell would interpret is allowed in it.
_CONFIG_PATH_RX = re.compile(r"^(~/)?[A-Za-z0-9_./-]+$")
_active: ContextVar[str | None] = ContextVar("active_system", default=None)
_CONFIG_TTL_S = 120
_config_cache: dict[str, tuple[float, dict, str]] = {}


def systems() -> dict[str, dict]:
    """system name -> {name, site, description, host, config}. Validated on every read."""
    doc = _systems_doc()
    comps = _systems()
    out: dict[str, dict] = {}
    for sname, spec in (doc.get("systems") or {}).items():
        if not isinstance(spec, dict):
            continue
        host = spec.get("host")
        if host is not None:
            host = str(host)
            if (comps.get(host) or {}).get("system") != sname:
                raise InventoryError(f"system {sname!r}: host {host!r} is not one of its "
                                     f"components")
            if not (comps[host].get("endpoint") or {}).get("host"):
                raise InventoryError(f"system {sname!r}: host {host!r} has no address")
        cfg = spec.get("config")
        if cfg is not None:
            cfg = str(cfg)
            if not _CONFIG_PATH_RX.match(cfg) or ".." in cfg.split("/"):
                raise InventoryError(
                    f"system {sname!r}: config {cfg!r} must be a plain path — letters, "
                    f"digits, _ . / - and an optional leading ~/")
        out[str(sname)] = {"name": str(sname), "site": spec.get("site"),
                           "description": spec.get("description"),
                           "host": host, "config": cfg}
    return out


def active() -> str | None:
    """The system the current session is about, if one was chosen."""
    return _active.get()


@contextmanager
def using(system: str | None):
    """Scope everything below to one system. None means unscoped, as before."""
    if system is not None and system not in systems():
        raise KeyError(f"unknown system {system!r}; known: {sorted(systems())}")
    token = _active.set(system)
    try:
        yield
    finally:
        _active.reset(token)


def system_required() -> bool:
    """A session must name its system once any system is reached over SSH.

    Before that — one machine, gotcha running locally — the old unscoped behaviour is
    still correct, and asking "which system?" would only be friction.
    """
    return any(s["host"] for s in systems().values())


def pick_system(text: str, explicit: str | None = None,
                remembered: str | None = None) -> tuple[str | None, str]:
    """Which system a request is about: (name, "") or (None, why-it-must-ask).

    (None, "") means "no system needed" — unscoped, the single-machine case. Order:
    an explicit choice, a system named in the text, one remembered from the same
    thread or conversation, the only system there is. Never a guess between several.
    """
    known = systems()
    if explicit:
        if explicit not in known:
            return None, f"There is no system called {explicit!r}. Known: {', '.join(sorted(known))}."
        return explicit, ""
    named = sorted(n for n in known
                   if re.search(rf"(?<![\w-]){re.escape(n)}(?![\w-])", text or "", re.I))
    if len(named) == 1:
        return named[0], ""
    if len(named) > 1:
        return None, (f"That mentions {' and '.join(named)}. Ask about one system at a "
                      f"time, so every check runs against the right machine.")
    if remembered in known:
        return remembered, ""
    if len(known) == 1:
        return next(iter(known)), ""
    if not system_required():
        return None, ""
    return None, (f"Which system is this about? Name it in the message — "
                  f"{', '.join(sorted(known))}.")


def config_nodes(system: str) -> tuple[dict[str, dict], str]:
    """(nodes the system's own gotcha config declares, error or "").

    Mock mode reads the local config, so fixture runs keep the node names the fixtures
    were recorded with. Live mode reads the file from the system's host over SSH, and
    keeps it for a couple of minutes: a run asks for it on every step.
    """
    import transport                            # transport imports this module
    spec = systems().get(system) or {}
    if transport.MODE == "mock":
        nodes = _gotcha30_nodes()
        return {n: {**r, "system": system} for n, r in nodes.items()}, ""
    if not spec.get("config"):
        return {}, ""
    hit = _config_cache.get(system)
    if hit and time.time() - hit[0] < _CONFIG_TTL_S:
        return hit[1], hit[2]
    try:
        doc = yaml.safe_load(transport.read_config(system)) or {}
        nodes = {n: {**r, "system": system} for n, r in _nodes_from_config(doc).items()}
        err = "" if nodes else f"{spec['config']} on {spec['host']} declares no nodes"
    except Exception as e:                      # noqa: BLE001 - reported, not fatal
        nodes, err = {}, f"could not read {spec['config']} on {spec['host']}: {e}"
    _config_cache[system] = (time.time(), nodes, err)
    return nodes, err


def view() -> dict[str, dict]:
    """What the current session may address: one system while one is active, else all."""
    sysname = active()
    if not sysname:
        return load()
    own = {n: r for n, r in load().items() if r.get("system") == sysname}
    nodes, _ = config_nodes(sysname)
    # The inventory overlays the config, as load() does: a node listed in both keeps
    # the config's type and gains the inventory's site, address and access.
    merged = {n: {k: r.get(k) for k in _PUBLIC_FIELDS} for n, r in nodes.items()}
    for n, r in own.items():
        merged[n] = {**merged.get(n, {}), **{k: v for k, v in r.items() if v is not None}}
    return merged


@lru_cache(maxsize=1)
def load() -> dict[str, dict]:
    """logical name -> public record. Cached; one registry per process.

    The systems file overlays the deployment config on matching names, so a node the
    launcher runs can be enriched with a site and access details without being listed
    twice. Only _PUBLIC_FIELDS survive.
    """
    systems()                                   # validate host/config with everything else
    out = _gotcha30_nodes()
    for name, rec in _systems().items():
        merged = {**out.get(name, {}), **{k: v for k, v in rec.items() if v is not None}}
        out[name] = merged
    return {n: {k: r.get(k) for k in _PUBLIC_FIELDS} for n, r in out.items()}


def names() -> list[str]:
    return sorted(view())


def require(name: str) -> dict:
    """Resolve a logical name or refuse. The model cannot reach anything not in here —
    and while a system is active, nothing outside that system."""
    inv = view()
    if name not in inv:
        where = f" in system {active()!r}" if active() else ""
        raise KeyError(f"unknown node {name!r}{where}; known nodes: {sorted(inv)}")
    return inv[name]


def domain_of(name: str) -> str | None:
    """The hardware domain a system declares, if the central registry declares one."""
    return (view().get(name) or {}).get("domain")


def as_prompt() -> str:
    lines = []
    if sysname := active():
        spec = systems()[sysname]
        lines.append(f"System under diagnosis: {sysname}"
                     + (f" (site {spec['site']})" if spec.get("site") else "")
                     + (f", gotcha running on {spec['host']}" if spec.get("host") else "")
                     + ". Only its nodes are listed, and only they can be reached.")
        if err := config_nodes(sysname)[1]:
            lines.append(f"WARNING: {err} — nodes it declares are missing from this list.")
    for n in sorted(view().values(), key=lambda r: r["name"]):
        ep = n["endpoint"]
        addr = (f" at {ep['scheme']}://{ep['host']}"
                + (f":{ep['port']}" if ep.get("port") else "")) if ep else ""
        loc = n.get("location")
        # Operator-defined fields go in as `key=value`. They exist because somebody
        # decided the agent needs them — an azimuth it cannot see is a field nobody
        # bothered to fill in.
        extra = [f"{k}={v}" for k, v in sorted((n.get("fields") or {}).items())
                 if v not in (None, "")]
        bits = [x for x in (n.get("hardware"),
                            f"v{n['software_version']}" if n.get("software_version") else None,
                            f"at {loc['lat']:.5f},{loc['lon']:.5f}" if loc else None,
                            *extra,
                            n.get("description")) if x]
        tail = f" — {' · '.join(bits)}" if bits else ""
        lines.append(f"- {n['name']} ({n['type']}, {n['group']}){addr}{tail}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# private view — transport.py only
# ---------------------------------------------------------------------------

def has_credentials(name: str) -> bool:
    return name in _access()


def credentials(name: str) -> dict:
    """Live access details for one system. NEVER put the result into graph state.

    Resolved fresh on every call and deliberately not cached: the secret exists as a
    Python object for the duration of one command and nowhere else. The password comes
    back as a Secret, so the only way to obtain the characters is an explicit reveal().

    Called from transport.py and nowhere else — asserted by a test, so the credential
    path has exactly one chokepoint, the same way transport is the only place a command
    runs.
    """
    rec = require(name)
    acc = _access().get(name)
    if not acc:
        raise KeyError(f"{name!r} has no access block in {SYSTEMS.name}; "
                       f"nothing to log into")
    host = (rec.get("endpoint") or {}).get("host")
    if not host:
        raise KeyError(f"{name!r} has no address; cannot reach it")

    password = None
    if env := acc.get("password_env"):
        # Environment first, then the console's local store. An operator who exports
        # the variable never has to write the secret to disk at all.
        if value := secrets_store.get(env):
            src = "env" if secrets_store.is_in_environment(env) else secrets_store.PATH.name
            password = Secret(value, f"{src}:{env}")
    key_file = os.path.expanduser(acc["key_file"]) if acc.get("key_file") else None
    return {"host": host, "user": acc["user"], "port": acc["port"],
            "key_file": key_file, "password": password,
            "password_env": acc.get("password_env")}
