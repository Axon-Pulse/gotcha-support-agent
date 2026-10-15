"""Each system, its components by category, and how to reach them: edited field by field from
the Secrets tab.

Every system has the same four categories. Which components are in each, and what is known
about each one, varies: a managed switch has an address and a password, an unmanaged one has
neither, a radar has fields a camera doesn't. So apart from its name and category, every field
of a component is optional, and any component can carry extra fields of its own.

Two files at the repo root, both gitignored, in the shape the retired bot used:

  systems_inventory.yaml
    systems:
      <system>:
        site, description
        components:
          <component>:
            domain: radar | acoustic | optic | infra     (the category)
            type, role, scheme, address, port
            access: {ssh: {user, password_env}}
            fields: {<name>: <value>, ...}                (extra fields anyone signed in sees)
            secret_fields: [{name: <name>, env: <NAME>}]  (extra fields kept secret)
  secrets.local.env
    NAME=value for every password_env and secret field's env

What everyone signed in sees: names, categories, plain fields, and which secrets are set. A
secret value leaves this module only through secret(), which server.py calls for an admin with
a fresh code, and only for an env name the component itself holds. Env names are always made
here, never taken from the page, so a component can't be pointed at another entry of
secrets.local.env (the Anthropic key lives there too); nothing reads the process environment.

There is deliberately no way to read or write the YAML as a whole from the page.

The YAML is read and written round-trip (ruamel.yaml): a change touches only what it
changes, and the rest of the file (comments, order, quoting, flow or block style) stays as
it was. Values the console writes are quoted whenever an older YAML reader (PyYAML, which the
retired bot used, reads YAML 1.1) would take them for something else: `yes`, `010`, `1.0`.
"""
import hashlib
import io
import ipaddress
import re
from collections import Counter

import yaml
from ruamel.yaml import YAML
from ruamel.yaml.error import YAMLError
from ruamel.yaml.representer import RoundTripRepresenter
from ruamel.yaml.scalarstring import ScalarString, SingleQuotedScalarString

from console.files import (env_entries, env_update, file_lock, meta_load, meta_save,
                           meta_touch, write_private)
from console.safe_text import has_control
from console.settings import S

# The four categories, in the order the page shows them: key -> (label, domain written).
CATEGORIES = {"radars": ("Radars", "radar"), "acoustics": ("Acoustics", "acoustic"),
              "cameras": ("Cameras", "optic"), "compute": ("Compute & Infrastructure", "infra")}
# What a component's domain says about its category. Anything else, or none (the retired bot
# didn't give servers, NUCs and processors one), is compute & infrastructure.
_DOMAIN_CATEGORY = {"radar": "radars", "acoustic": "acoustics", "acoustics": "acoustics",
                    "optic": "cameras", "camera": "cameras", "cameras": "cameras"}

NAME_RX = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
FIELD_RX = re.compile(r"[A-Za-z0-9][A-Za-z0-9 ._-]{0,39}")
USER_RX = re.compile(r"[A-Za-z_][A-Za-z0-9_-]{0,31}")
SCHEME_RX = re.compile(r"[a-z][a-z0-9+.-]{0,15}")
HOST_RX = re.compile(r"(?=.{1,253}$)[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
                     r"(\.[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*")
ENV_RX = re.compile(r"[A-Z][A-Z0-9_]{0,63}")
# A plain field with one of these names would put a secret where viewers see it.
# Whole words only, so "compass heading" or "keyframe interval" are fine and "admin pw" isn't.
SECRET_WORDS = re.compile(r"(^|[\s._-])(pass|passwords?|passphrase|passcode|passwd|pwd|pw|psk|secrets?"
                          r"|tokens?|api[\s._-]?keys?|keys?|credentials?|community|pin|auth|otp|private)"
                          r"($|[\s._-])", re.I)
MAX_TEXT = 200
MAX_SECRET = 4096
MAX_FIELDS = 40
SECRETS_HEADER = ("# Device passwords and secret fields, by the NAME systems_inventory.yaml gives\n"
                  "# them. Edited from the console's Secrets tab. Never commit.\n")
SYSTEM_TEXT = ("site", "description")
COMPONENT_TEXT = ("type", "role")


class AccessError(ValueError):
    """A change this console won't write. The message is safe to show."""


class NotFound(AccessError):
    """No such system, component or field."""


class Conflict(AccessError):
    """The name is taken."""


# --------------------------------------------------------------------------
# Files
# --------------------------------------------------------------------------

class _TildeNull(RoundTripRepresenter):
    """Writes an empty value as ~, for files that already write it that way."""


_TildeNull.add_representer(type(None), lambda r, _: r.represent_scalar("tag:yaml.org,2002:null", "~"))


def _rt(null: str = "null") -> YAML:
    y = YAML(typ="rt")
    y.preserve_quotes = True
    y.width = 4096  # never fold a long value onto a second line
    if null == "~":
        y.Representer = _TildeNull
    return y


def _null_style() -> str:
    try:
        text = S.inventory_file.read_text(encoding="utf-8")
    except FileNotFoundError:
        return "null"
    return "~" if len(re.findall(r":\s+~\s*(?:[,}\n#]|$)", text)) > text.count("null") else "null"


def _dump(doc: dict, null: str | None = None) -> str:
    buf = io.StringIO()
    _rt(null or _null_style()).dump(doc, buf)
    return buf.getvalue()


def _w(v):
    """A string as the console writes it: quoted when a YAML 1.1 reader would read it as
    anything but this string."""
    if isinstance(v, str) and not isinstance(v, ScalarString):
        try:
            same = yaml.safe_load(v) == v
        except yaml.YAMLError:
            same = False
        if not same:
            return SingleQuotedScalarString(v)
    return v


def _doc() -> dict:
    try:
        d = _rt().load(S.inventory_file.read_text(encoding="utf-8")) or {}
    except FileNotFoundError:
        return {}
    except YAMLError as e:
        # Only where, not the offending text: that line would be shown to viewers.
        mark = getattr(e, "problem_mark", None)
        where = f" (line {mark.line + 1})" if mark is not None else ""
        raise AccessError(f"{S.inventory_file.name} is not valid YAML{where}; fix it on the server") from None
    if not isinstance(d, dict):
        raise AccessError(f"{S.inventory_file.name} must be a mapping with `systems:`")
    return d


def _save_doc(doc: dict) -> None:
    write_private(S.inventory_file, _dump(doc))


def _secrets() -> dict[str, str]:
    """NAME -> value, read the way the retired bot's secrets_store reads it (raw values)."""
    return {k: v for k, v in env_entries(S.device_secrets_file) if ENV_RX.fullmatch(k)}


def _save_secrets(changes: dict[str, str | None]) -> None:
    """Set (value) or remove (None) entries, leaving every other line of the file as it was:
    its comments, and entries the console doesn't manage."""
    env_update(S.device_secrets_file,
               {k: (None if v is None else f"{k}={v}") for k, v in changes.items()},
               header=SECRETS_HEADER)


# --------------------------------------------------------------------------
# Shapes (a hand-edited file may hold anything; reading never fails on it)
# --------------------------------------------------------------------------

def _systems(doc: dict) -> dict:
    s = doc.get("systems")
    if s is None:
        s = doc["systems"] = {}
    if not isinstance(s, dict):
        raise AccessError("`systems:` in the inventory is not a mapping; fix it on the server")
    return s


def _components(sysd: dict) -> dict:
    c = sysd.get("components")
    if c is None:
        c = sysd["components"] = {}
    if not isinstance(c, dict):
        raise AccessError("a system's `components:` is not a mapping; fix it on the server")
    return c


def _ssh(comp: dict) -> dict:
    a = comp.get("access")
    s = a.get("ssh") if isinstance(a, dict) else None
    return s if isinstance(s, dict) else {}


def _ssh_env(comp) -> str | None:
    env = _ssh(comp).get("password_env") if isinstance(comp, dict) else None
    return env if isinstance(env, str) and env else None


def _plain_fields(comp: dict) -> dict:
    f = comp.get("fields")
    return {str(k): ("" if v is None else str(v)) for k, v in f.items()} if isinstance(f, dict) else {}


def _secret_fields(comp: dict) -> list[dict]:
    """[{name, env}] for the well-formed entries of `secret_fields`."""
    out = []
    raw = comp.get("secret_fields") if isinstance(comp, dict) else None
    for e in (raw if isinstance(raw, list) else ()):
        if isinstance(e, dict) and isinstance(e.get("name"), str) and isinstance(e.get("env"), str) \
                and ENV_RX.fullmatch(e["env"]):
            out.append({"name": e["name"], "env": e["env"]})
    return out


def _envs(comp) -> set[str]:
    """Every secrets-file entry this component holds."""
    if not isinstance(comp, dict):
        return set()
    return ({_ssh_env(comp)} - {None}) | {e["env"] for e in _secret_fields(comp)}


def _category_of(comp) -> str:
    domain = comp.get("domain") if isinstance(comp, dict) else None
    return _DOMAIN_CATEGORY.get(str(domain).lower(), "compute") if domain else "compute"


def _key(mapping: dict, name: str):
    """The key in `mapping` whose text is `name` (the YAML may use 1 as well as "1")."""
    return next((k for k in mapping if str(k) == name), None)


def _yaml_key(name: str):
    """How a new name is written: a plain number stays a number, like the existing keys."""
    return int(name) if name.isdigit() and not name.startswith("0") else _w(name)


def _rename(mapping: dict, old, new) -> None:
    """Rename a key where it is, keeping its place (and, in a round-trip mapping, the
    comments around it)."""
    pos = list(mapping).index(old)
    value = mapping.pop(old)
    if hasattr(mapping, "insert"):
        mapping.insert(pos, new, value)
    else:
        items = list(mapping.items())
        mapping.clear()
        mapping.update(items[:pos] + [(new, value)] + items[pos:])


def _not_shared(doc: dict, node, where: str) -> None:
    """Refuse to change a mapping that YAML anchors (&a / *a) share with another place in the
    file: editing it here would silently change the other place too."""
    seen: Counter = Counter()

    def walk(n):
        if isinstance(n, (dict, list)):
            seen[id(n)] += 1
            if seen[id(n)] == 1:
                for child in (n.values() if isinstance(n, dict) else n):
                    walk(child)
    walk(doc)

    def check(n):
        if isinstance(n, (dict, list)):
            if seen[id(n)] > 1:
                raise AccessError(f"{where} shares part of its YAML with another entry (an anchor); "
                                  "change it on the server")
            for child in (n.values() if isinstance(n, dict) else n):
                check(child)
    check(node)


def _system(doc: dict, system: str) -> tuple[object, dict]:
    systems = _systems(doc)
    k = _key(systems, system)
    if k is None:
        raise NotFound(f"no system {system}")
    if systems[k] is None:
        systems[k] = {}
    if not isinstance(systems[k], dict):
        raise AccessError(f"system {system} is not a mapping; fix it on the server")
    return k, systems[k]


def _component(doc: dict, system: str, component: str) -> tuple[dict, object, dict]:
    _, sysd = _system(doc, system)
    comps = _components(sysd)
    k = _key(comps, component)
    if k is None:
        raise NotFound(f"no component {component} in system {system}")
    if comps[k] is None:
        comps[k] = {}
    if not isinstance(comps[k], dict):
        raise AccessError(f"{system}/{component} is not a mapping; fix it on the server")
    return comps, k, comps[k]


def _env_refs(doc: dict) -> dict[str, int]:
    """How many components hold each secrets-file entry."""
    refs: dict[str, int] = {}
    systems = doc.get("systems")
    for sysd in (systems.values() if isinstance(systems, dict) else ()):
        comps = sysd.get("components") if isinstance(sysd, dict) else None
        for c in (comps.values() if isinstance(comps, dict) else ()):
            for env in _envs(c):
                refs[env] = refs.get(env, 0) + 1
    return refs


def _env_name(system: str, component: str, what: str, taken: set[str]) -> str:
    """A new env name: SYSTEM_COMPONENT_WHAT_PW, shortened with a hash when long so that it
    always is a name the secrets file keeps (ENV_RX, 64 characters at most)."""
    raw = f"{system}_{component}_{what}"
    base = re.sub(r"[^A-Z0-9]+", "_", raw.upper()).strip("_")
    if not base[:1].isalpha():
        base = f"SYS_{base}"
    if len(base) > 48:
        base = f"{base[:41].rstrip('_')}_{hashlib.sha256(raw.encode()).hexdigest()[:6].upper()}"
    name = f"{base}_PW"
    n, out = 2, name
    while out in taken:
        out, n = f"{name}_{n}", n + 1
    return out


# --------------------------------------------------------------------------
# Checks: every value the page sends goes through one of these
# --------------------------------------------------------------------------

def _name(v, what: str) -> str:
    v = str(v or "").strip()
    if not NAME_RX.fullmatch(v):
        raise AccessError(f"a {what} name is 1-64 letters, digits, '.', '_' or '-', "
                          "starting with a letter or digit")
    return v


def _text(v, what: str) -> str:
    v = str(v or "").strip()
    if len(v) > MAX_TEXT or has_control(v):
        raise AccessError(f"the {what} is one line of at most {MAX_TEXT} characters")
    return v


def _address(v) -> str:
    v = str(v or "").strip()
    try:
        return str(ipaddress.ip_address(v))
    except ValueError:
        if HOST_RX.fullmatch(v):
            return v
    raise AccessError(f"{v!r} is not an IP address or host name")


def _port(v) -> int:
    if isinstance(v, bool) or not isinstance(v, int) or not 1 <= v <= 65535:
        raise AccessError("the port is a number from 1 to 65535")
    return v


def _scheme(v) -> str:
    v = str(v or "").strip().lower()
    if not SCHEME_RX.fullmatch(v):
        raise AccessError("a scheme is like http, https, ws or rtsp")
    return v


def _user(v) -> str:
    v = str(v or "").strip()
    if not USER_RX.fullmatch(v):
        raise AccessError("an SSH user is letters, digits, '_' and '-', starting with a letter or '_'")
    return v


def _secret_value(v, what: str) -> str:
    if not isinstance(v, str) or not v:
        raise AccessError(f"the {what} is empty; remove it instead")
    if len(v) > MAX_SECRET or has_control(v):
        raise AccessError(f"the {what} has a line break or another control character in it, "
                          "or is too long")
    return v


def _category(v) -> str:
    if v not in CATEGORIES:
        raise AccessError("the category is one of: " + ", ".join(label for label, _ in CATEGORIES.values()))
    return v


def _field_name(v) -> str:
    v = str(v or "").strip()
    if not FIELD_RX.fullmatch(v):
        raise AccessError(f"a field name is 1-40 letters, digits, spaces, '.', '_' or '-' ({v!r})")
    return v


def _shown(v) -> str:
    """A plain field's value as the page shows and sends it back."""
    return "" if v is None else str(v)


def _plain(fields, existing=None) -> dict:
    """The new set of plain fields, from what the page sent. A field sent back exactly as it
    was shown is kept exactly as the file has it (its type, quoting, even a value the console
    wouldn't write); only new or changed ones are checked. An emptied field is removed."""
    if not isinstance(fields, dict):
        raise AccessError("fields must be a mapping of name to value")
    if len(fields) > MAX_FIELDS:
        raise AccessError(f"at most {MAX_FIELDS} extra fields")
    old = {str(k): (k, v) for k, v in existing.items()} if isinstance(existing, dict) else {}
    out, names = {}, set()
    for raw_name, v in fields.items():
        name = str(raw_name)
        if name in old and _shown(old[name][1]) == v:
            key, val = old[name]
        else:
            if v in ("", None):
                continue
            key = _field_name(raw_name)
            if SECRET_WORDS.search(key):
                raise AccessError(f"“{key}” sounds like a secret; add it as a secret field so it stays hidden")
            val = _w(_text(v, f"value of {key}"))
            key = _w(key)
        if str(key).lower() in names:
            raise AccessError(f"two fields are called {key}")
        names.add(str(key).lower())
        out[key] = val
    return out


def _secret_changes(v) -> dict:
    if not isinstance(v, dict):
        raise AccessError("secret_fields must be a mapping of name to value (or null to remove)")
    out = {}
    for raw_name, val in v.items():
        k = _field_name(raw_name)
        if k.lower() in (x.lower() for x in out):
            raise AccessError(f"two secret fields are called {k}")
        out[k] = None if val is None else _secret_value(val, f"secret field {k}")
    return out


def current_fields(system: str, component: str) -> dict | None:
    """A component's plain fields as the file has them, or None (for precheck)."""
    try:
        _, _, comp = _component(_doc(), system, component)
    except AccessError:
        return None
    f = comp.get("fields")
    return f if isinstance(f, dict) else None


def precheck(changes: dict, fields_now: dict | None = None) -> None:
    """Check the shape of every value sent, before the admin's code is spent on it. What
    depends on the file (a name taken, a system gone) is checked again when it's applied.
    `fields_now` is the component's current plain fields, so those sent back unchanged pass."""
    if changes.get("password") and changes.get("clear_password"):
        raise AccessError("either set a new password or remove it, not both")
    checks = {"name": lambda v: _name(v, "system or component"), "category": _category,
              "site": lambda v: _text(v, "location"), "description": lambda v: _text(v, "description"),
              "type": lambda v: _text(v, "type"), "role": lambda v: _text(v, "role"),
              "scheme": _scheme, "address": _address, "port": _port, "user": _user,
              "password": lambda v: _secret_value(v, "password"),
              "fields": lambda v: _plain(v, fields_now), "secret_fields": _secret_changes}
    for k, v in changes.items():
        # A name and a category are always checked; anything else only when it has a value
        # ("" or None means "remove it").
        if k in checks and (k in ("name", "category") or v not in ("", None)):
            checks[k](v)


# --------------------------------------------------------------------------
# Reading
# --------------------------------------------------------------------------

def listing() -> dict:
    exists = S.inventory_file.exists()
    try:
        doc = _doc()
    except AccessError as e:
        return {"systems": [], "inventory_file": exists, "error": str(e), "categories": _cats()}
    secrets, meta = _secrets(), meta_load(S.access_meta_file)
    systems = []
    raw_systems = doc.get("systems")
    for sname, raw_sys in (raw_systems.items() if isinstance(raw_systems, dict) else ()):
        sysd = raw_sys if isinstance(raw_sys, dict) else {}
        by_cat: dict[str, list] = {k: [] for k in CATEGORIES}
        raw = sysd.get("components")
        for cname, raw_comp in (raw.items() if isinstance(raw, dict) else ()):
            c = raw_comp if isinstance(raw_comp, dict) else {}
            env = _ssh_env(c)
            m = meta.get(f"{sname}/{cname}", {})
            by_cat[_category_of(c)].append({
                "name": str(cname), "type": c.get("type"), "role": c.get("role"),
                "scheme": c.get("scheme"), "address": c.get("address"), "port": c.get("port"),
                "user": _ssh(c).get("user"), "has_password": bool(env and secrets.get(env)),
                "fields": _plain_fields(c),
                "secret_fields": [{"name": e["name"], "set": bool(secrets.get(e["env"]))}
                                  for e in _secret_fields(c)],
                "updated_at": m.get("at"), "updated_by": m.get("by")})
        m = meta.get(str(sname), {})
        systems.append({"name": str(sname), "site": sysd.get("site"),
                        "description": sysd.get("description"),
                        "categories": [{"key": k, "label": CATEGORIES[k][0], "components": v}
                                       for k, v in by_cat.items()],
                        "updated_at": m.get("at"), "updated_by": m.get("by")})
    return {"systems": systems, "inventory_file": exists, "categories": _cats()}


def _cats() -> list[dict]:
    return [{"key": k, "label": label} for k, (label, _) in CATEGORIES.items()]


def secret(system: str, component: str, field: str | None = None) -> str | None:
    """The SSH password (field None), or one secret field's value."""
    _, _, comp = _component(_doc(), system, component)
    if field is None:
        env = _ssh_env(comp)
    else:
        env = next((e["env"] for e in _secret_fields(comp) if e["name"] == field), None)
        if env is None:
            raise NotFound(f"{system}/{component} has no secret field {field}")
    return _secrets().get(env) if env else None


# --------------------------------------------------------------------------
# Systems
# --------------------------------------------------------------------------

def add_system(name: str, who: str, site: str = "", description: str = "") -> str:
    name = _name(name, "system")
    fields = {"site": _text(site, "location"), "description": _text(description, "description")}
    with file_lock("access"):
        doc = _doc()
        systems = _systems(doc)
        if _key(systems, name) is not None:
            raise Conflict(f"there is already a system called {name}")
        systems[_yaml_key(name)] = {**{k: _w(v) for k, v in fields.items() if v}, "components": {}}
        meta = meta_load(S.access_meta_file)
        meta_touch(meta, name, who)
        _save_doc(doc)
        meta_save(S.access_meta_file, meta)
    return name


def edit_system(system: str, who: str, changes: dict) -> list[str]:
    """`changes` holds only the fields to change: name, site, description ("" removes one)."""
    with file_lock("access"):
        doc = _doc()
        k, sysd = _system(doc, system)
        _not_shared(doc, sysd, system)
        changed = []
        for f in SYSTEM_TEXT:
            if f in changes:
                v = _text(changes[f], "location" if f == "site" else f)
                if v != (sysd.get(f) or ""):
                    if v:
                        sysd[f] = _w(v)
                    else:
                        sysd.pop(f, None)
                    changed.append(f)
        new = system
        if "name" in changes:
            new = _name(changes["name"], "system")
            if new != system:
                systems = _systems(doc)
                if _key(systems, new) is not None:
                    raise Conflict(f"there is already a system called {new}")
                _rename(systems, k, _yaml_key(new))
                changed.append("name")
        if not changed:
            return []
        meta = meta_load(S.access_meta_file)
        if new != system:
            for mk in list(meta):
                if mk == system or mk.startswith(system + "/"):
                    meta[new + mk[len(system):]] = meta.pop(mk)
        meta_touch(meta, new, who)
        _save_doc(doc)
        meta_save(S.access_meta_file, meta)
    return changed


def delete_system(system: str) -> int:
    """Remove a system, and the secrets no other component holds. Returns how many components
    went with it."""
    with file_lock("access"):
        doc = _doc()
        k, sysd = _system(doc, system)
        comps = sysd.get("components") if isinstance(sysd.get("components"), dict) else {}
        envs = set().union(*(_envs(c) for c in comps.values())) if comps else set()
        del _systems(doc)[k]
        _save_doc(doc)
        _drop_unused(doc, envs)
        meta = meta_load(S.access_meta_file)
        for mk in [m for m in meta if m == system or m.startswith(system + "/")]:
            meta.pop(mk)
        meta_save(S.access_meta_file, meta)
    return len(comps)


def _drop_unused(doc: dict, envs: set[str]) -> None:
    refs, held = _env_refs(doc), _secrets()
    _save_secrets({e: None for e in envs if not refs.get(e) and e in held})


# --------------------------------------------------------------------------
# Components
# --------------------------------------------------------------------------

def _apply(system: str, cname: str, comp: dict, changes: dict, doc: dict) -> tuple[list[str], dict]:
    """Write `changes` into `comp` in memory. Every field is optional: one left out is left
    alone, "" or None removes it. The SSH password is set with `password` and removed with
    `clear_password`; `fields` replaces the plain extra fields; `secret_fields` maps a name to
    a new value, or to None to remove it. Returns the fields changed and the secrets-file
    changes, which the caller saves after the YAML."""
    changed: list[str] = []
    secrets_out: dict[str, str | None] = {}
    held = _secrets()
    refs = _env_refs(doc)

    def put(key, value, holder):
        if value in ("", None):
            if key in holder:
                holder.pop(key)
                changed.append(key)
        elif holder.get(key) != value:
            holder[key] = _w(value)
            changed.append(key)

    if "category" in changes:
        cat = _category(changes["category"])
        if _category_of(comp) != cat or comp.get("domain") != CATEGORIES[cat][1]:
            comp["domain"] = _w(CATEGORIES[cat][1])
            changed.append("category")
    for f in COMPONENT_TEXT:
        if f in changes:
            put(f, _text(changes[f], f), comp)
    if "scheme" in changes:
        put("scheme", _scheme(changes["scheme"]) if changes["scheme"] else "", comp)
    if "address" in changes:
        put("address", _address(changes["address"]) if changes["address"] else "", comp)
    if "port" in changes:
        put("port", _port(changes["port"]) if changes["port"] not in ("", None) else None, comp)

    def shared(env):
        if env and refs.get(env, 0) > 1:
            # Changing it here would change every component that holds it.
            raise AccessError(f"{env} is shared with another component; change it on the server")

    # SSH user and password
    want_pw = changes.get("password") not in (None, "")
    if want_pw:
        _secret_value(changes["password"], "password")
    user = _user(changes["user"]) if changes.get("user") else ""
    if user or want_pw:
        access = comp.setdefault("access", {})
        if not isinstance(access, dict) or not isinstance(access.setdefault("ssh", {}), dict):
            raise AccessError(f"{system}/{cname} has an `access:` that is not a mapping; fix it on the server")
    ssh = _ssh(comp)
    if "user" in changes:
        put("user", user, ssh)
    if want_pw or changes.get("clear_password"):
        env = ssh.get("password_env")
        if env is not None and not (isinstance(env, str) and ENV_RX.fullmatch(env)):
            raise AccessError(f"{system}/{cname} has a password_env that isn't a valid name; "
                              "fix it on the server")
        shared(env)
        if want_pw:
            if not env:
                env = _env_name(system, cname, "SSH", set(held) | set(refs))
                ssh["password_env"] = _w(env)
                changed.append("password_env")
            if held.get(env) != changes["password"]:
                secrets_out[env] = changes["password"]
                changed.append("password")
        elif env:
            # Removed for good: the value, and the name that pointed at it.
            ssh.pop("password_env", None)
            if env in held:
                secrets_out[env] = None
            changed.append("password")

    # A field name is one field: a plain one can't take the name of a secret one, or the
    # other way round, whatever the case (secrets being removed in this request don't count).
    sec_changes = _secret_changes(changes["secret_fields"]) if changes.get("secret_fields") else {}
    removed = {k.lower() for k, v in sec_changes.items() if v is None}
    secret_names = {e["name"].lower() for e in _secret_fields(comp)} - removed

    # Plain extra fields: the page sends the whole set
    if changes.get("fields") is not None:
        existing = comp.get("fields")
        new = _plain(changes["fields"], existing)
        adding = {x.lower() for x, v in sec_changes.items() if v is not None}
        clash = [k for k in new if str(k).lower() in secret_names | adding]
        if clash:
            raise AccessError(f"{clash[0]} is already a secret field")
        if {str(k): _shown(v) for k, v in new.items()} != _plain_fields(comp):
            if not new:
                comp.pop("fields", None)
            elif isinstance(existing, dict):
                # In place, so the mapping keeps its style ({a: 1} stays on one line).
                for k in [k for k in existing if k not in new]:
                    del existing[k]
                for k, v in new.items():
                    if k not in existing or existing[k] is not v:
                        existing[k] = v
            else:
                comp["fields"] = new
            changed.append("fields")

    # Secret extra fields: name -> new value, or None to remove
    if sec_changes:
        entries = _secret_fields(comp)
        listed = [(e["name"], e["env"]) for e in entries]
        plain = {k.lower() for k in _plain_fields(comp)}
        for fname, val in sec_changes.items():
            entry = next((e for e in entries if e["name"].lower() == fname.lower()), None)
            if val is None:
                if entry:
                    shared(entry["env"])
                    entries.remove(entry)
                    secrets_out[entry["env"]] = None
                    changed.append(f"secret:{fname}")
                continue
            if entry is None:
                if fname.lower() in plain:
                    raise AccessError(f"{fname} is already a plain field")
                if len(entries) + len(_plain_fields(comp)) >= MAX_FIELDS:
                    raise AccessError(f"at most {MAX_FIELDS} extra fields")
                entry = {"name": fname, "env": _env_name(system, cname, fname,
                                                         set(held) | set(refs) | set(secrets_out))}
                entries.append(entry)
                changed.append(f"secret:{fname}")
            else:
                shared(entry["env"])
            if held.get(entry["env"]) != val:
                secrets_out[entry["env"]] = val
                if f"secret:{fname}" not in changed:
                    changed.append(f"secret:{fname}")
        # The list in the YAML changes only when a field was added or removed: a new value
        # alone goes to secrets.local.env and leaves the YAML as it was.
        if [(e["name"], e["env"]) for e in entries] != listed:
            if entries:
                comp["secret_fields"] = [{"name": _w(e["name"]), "env": _w(e["env"])} for e in entries]
            else:
                comp.pop("secret_fields", None)

    # No empty `access: {ssh: {}}` left behind.
    if isinstance(comp.get("access"), dict):
        if comp["access"].get("ssh") == {}:
            comp["access"].pop("ssh")
        if comp["access"] == {}:
            comp.pop("access")
    return changed, secrets_out


def add_component(system: str, name: str, who: str, fields: dict) -> list[str]:
    name = _name(name, "component")
    if "category" not in fields:
        raise AccessError("choose the component's category")
    with file_lock("access"):
        doc = _doc()
        _, sysd = _system(doc, system)
        comps = _components(sysd)
        if _key(comps, name) is not None:
            raise Conflict(f"{system} already has a component called {name}")
        comp: dict = {}
        changed, secrets_out = _apply(system, name, comp, fields, doc)
        comps[_yaml_key(name)] = comp
        meta = meta_load(S.access_meta_file)
        meta_touch(meta, f"{system}/{name}", who)
        _save_doc(doc)
        _save_secrets(secrets_out)
        meta_save(S.access_meta_file, meta)
    return changed


def edit_component(system: str, component: str, who: str, changes: dict) -> list[str]:
    """`changes` holds only the fields to change (see _apply), plus an optional new `name`."""
    with file_lock("access"):
        doc = _doc()
        before = _dump(doc)
        comps, k, comp = _component(doc, system, component)
        _not_shared(doc, comp, f"{system}/{component}")
        new = component
        if "name" in changes:
            new = _name(changes["name"], "component")
            if new != component and _key(comps, new) is not None:
                raise Conflict(f"{system} already has a component called {new}")
        changed, secrets_out = _apply(system, new, comp, changes, doc)
        if new != component:
            _rename(comps, k, _yaml_key(new))
            changed.insert(0, "name")
        if not changed:
            return []
        meta = meta_load(S.access_meta_file)
        meta.pop(f"{system}/{component}", None)
        meta_touch(meta, f"{system}/{new}", who)
        # Only when its data changed: a new password value alone leaves the file as it is.
        if _dump(doc) != before:
            _save_doc(doc)
        _save_secrets(secrets_out)
        meta_save(S.access_meta_file, meta)
    return changed


def delete_component(system: str, component: str) -> None:
    with file_lock("access"):
        doc = _doc()
        comps, k, comp = _component(doc, system, component)
        envs = _envs(comp)
        del comps[k]
        _save_doc(doc)
        _drop_unused(doc, envs)
        meta = meta_load(S.access_meta_file)
        meta.pop(f"{system}/{component}", None)
        meta_save(S.access_meta_file, meta)
