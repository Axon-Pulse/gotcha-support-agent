"""Command transport: the only place that runs anything, and the only place a
credential is ever revealed.

Commands are KEYS into a frozen table, never strings built from model input.
No shell=True, ever — ticket text and log lines are attacker-influenced input.

Mock vs live is resolved once, here. Tool code is mode-blind, so a fixture run
exercises byte-identical parser paths to a live run.

run_on() targets a system by LOGICAL NAME. The name is resolved through inventory,
which is the only authority for addresses; the model never supplies a host. The
argv carries `{host}` / `{ssh_user}` / `{ssh_port}` placeholders filled from that
record, and a password — if one is configured at all — goes into the child process
ENVIRONMENT, never onto an argv where `ps` would show it to every local user.
"""
import ipaddress
import os
import re
import shlex
import subprocess
from pathlib import Path

import config_store
import inventory

DEFAULT_ALLOWED: dict[str, tuple[str, ...]] = {
    "topology": ("ecal_mon_cli", "-l"),
    # -c must be reachable inside TIMEOUTS["health"] at the rate being sampled, or the
    # capture is cut mid-stream and loses whatever sat in an unflushed stdio block.
    # Measured on the bench: 6 nodes, one health message each per 4-8s, ~1.1 msg/s
    # aggregate. 24 messages arrive in ~22s — under the 30s net, and 2-4 samples per
    # node, enough for _cluster() to tell two launcher sessions from one.
    "health":   ("ecal_mon_cli", "--proto", "/system/health", "-c", "24"),
    "launcher": ("ecal_mon_cli", "--proto", "/launcher/status", "-c", "2"),
    # The ASU backend is a local Docker service, not a sensor on the LAN. `-a` is
    # load-bearing: without it an exited container is invisible, and "absent" and
    # "exited (137)" are different faults with different fixes.
    "asu_container": ("docker", "ps", "-a", "--filter", "name=dumbo",
                      "--format", "{{.Names}}\t{{.State}}\t{{.Status}}\t{{.Image}}"),
    "asu_api": ("curl", "-s", "-o", "/dev/null", "-w", "%{http_code}",
                "--max-time", "3", "http://localhost:8000/"),
    # Node-targeted, run through run_on(). Placeholders are filled from the inventory
    # record for a logical name — they are not free-form and cannot come from the model.
    "remote_uptime": ("ssh", "-p", "{ssh_port}", "{ssh_user}@{host}", "uptime"),
    # --- domain probes -----------------------------------------------------
    # Liveness only, until the real per-domain queries exist. They are here so the
    # permission surface for tools/radar.py, tools/camera.py and tools/tower.py is in
    # place and reviewable now, rather than being added under time pressure later.
    # All read-only: `uptime` prints, and curl discards the body.
    "radar_status":  ("ssh", "-p", "{ssh_port}", "{ssh_user}@{host}", "uptime"),
    "camera_status": ("curl", "-s", "-o", "/dev/null", "-w", "%{http_code}",
                      "--max-time", "3", "http://{host}/"),
    "tower_status":  ("ssh", "-p", "{ssh_port}", "{ssh_user}@{host}", "uptime"),
}



def allowed() -> dict[str, list]:
    """Effective command table: code defaults, overlaid with console edits."""
    return {k: list(v) for k, v in
            config_store.get("allowed_commands", DEFAULT_ALLOWED).items()}


# Which fixture file backs each command in mock mode.
FIXTURES = {
    "topology": "topics_faulty.txt",
    "health":   "health_faulty.txt",
    "launcher": "launcher_status.txt",
    "asu_container": "asu_container.txt",
    "asu_api": "asu_api.txt",
    "remote_uptime": "remote_uptime.txt",
    "radar_status": "radar_status.txt",
    "camera_status": "camera_status.txt",
    "tower_status": "tower_status.txt",
}

DEFAULT_TIMEOUT = 10
# Per-key timeout, seconds. The default suits a command that answers immediately.
# A streaming capture instead needs a window sized to the publish rate it samples:
# too short and the tool reports a thin sample as if it were the whole system, which
# is how a CRITICAL node once hid behind a one-sample capture. Keep in step with any
# `-c` count above — the count should bind first, the timeout is only the safety net.
TIMEOUTS: dict[str, int] = {"health": 30}

MODE = os.environ.get("AGENT_MODE", "mock")
FIXTURE_DIR = Path(os.environ.get("FIXTURE_DIR", "tests/fixtures"))


def run(key: str, timeout: int | None = None) -> str:
    """Run an allowlisted command (live) or read its fixture (mock).

    `timeout` defaults per key: see TIMEOUTS.
    """
    if timeout is None:
        timeout = TIMEOUTS.get(key, DEFAULT_TIMEOUT)
    table = allowed()
    if key not in table:
        raise KeyError(f"command {key!r} is not allowlisted: {sorted(table)}")
    if MODE == "mock":
        path = FIXTURE_DIR / FIXTURES.get(key, f"{key}.txt")
        if not path.exists():
            raise FileNotFoundError(f"no fixture for {key!r} at {path}")
        return path.read_text()
    return _here_or_there(list(table[key]), timeout)


def run_argv(argv: list[str], timeout: int = 10) -> str:
    """Run a validated argv list. Callers MUST have validated every element.

    Used only by net.py, whose arguments are parsed as ipaddress/int before arrival.
    """
    if not isinstance(argv, list) or not all(isinstance(a, str) for a in argv):
        raise TypeError("argv must be a list of str")
    return _here_or_there(argv, timeout)


# ---------------------------------------------------------------------------
# Where a command runs.
#
# Every check that reads "the system" — health, topology, the launcher, docker, the ASU
# API on localhost, ping and route — describes the machine it runs ON. On a central
# server that machine is the wrong one: the system is at a site, running gotcha on its
# own box. So while a session has a system active and that system names a host, these
# commands run on that host over SSH. With no host anywhere, nothing changes: they run
# here, which is right when this IS the gotcha machine.
# ---------------------------------------------------------------------------

def where() -> str | None:
    """The component commands run on, or None for this machine."""
    sysname = inventory.active()
    if not sysname:
        if inventory.system_required():
            raise RuntimeError("no system is selected for this session, and systems are "
                               "reached over SSH — refusing to diagnose this server instead")
        return None
    host = inventory.systems()[sysname]["host"]
    if not host and inventory.system_required():
        raise RuntimeError(f"system {sysname!r} has no host set in the inventory, so "
                           f"there is no machine to run its checks on")
    return host


def _here_or_there(argv: list[str], timeout: int) -> str:
    host = where()
    if not host:
        return subprocess.run(
            ["timeout", str(timeout), *argv],
            capture_output=True, text=True, timeout=timeout + 5,
        ).stdout
    return _remote(host, argv, timeout)


# One connection per host, reused for a minute: a run makes a dozen calls, and a fresh
# handshake to a remote site for each one is most of the run's wall-clock.
_CM_DIR = Path(__file__).resolve().parent / ".ssh-cm"


def _multiplex() -> list[str]:
    _CM_DIR.mkdir(mode=0o700, exist_ok=True)
    return ["-o", "ControlMaster=auto", "-o", f"ControlPath={_CM_DIR}/%C",
            "-o", "ControlPersist=60"]


def _login(node: str) -> tuple[list[str], dict, list[str], dict]:
    """(prefix, env, ssh argv through the destination, creds) for logging in to a node."""
    rec = inventory.require(node)
    host = str((rec.get("endpoint") or {}).get("host") or "")
    if not host:
        raise KeyError(f"{node!r} has no address in the inventory; nothing to probe")
    if host != "localhost":
        ipaddress.ip_address(host)               # an address, never a shell-shaped string
    creds = inventory.credentials(node)
    env, prefix = dict(os.environ), []
    opts = [*_SSH_HARDENING, *_multiplex()]
    if creds.get("password"):
        env["SSHPASS"] = creds["password"].reveal()
        prefix = ["sshpass", "-e"]
        opts += ["-o", "BatchMode=no", "-o", "PubkeyAuthentication=no"]
    else:
        opts += ["-o", "BatchMode=yes"]
        if creds.get("key_file"):
            opts += ["-i", creds["key_file"]]
    return prefix, env, ["ssh", *opts, "-p", str(creds["port"]),
                         f"{creds['user']}@{host}"], creds


def login_info(node: str) -> dict:
    """How this server would log in to a node, with no secret in it — for preflight.
    Kept here so credentials() still has exactly one caller."""
    creds = inventory.credentials(node)
    return {"user": creds["user"], "port": creds["port"], "key_file": creds["key_file"],
            "password_env": creds["password_env"],
            "has_password": creds["password"] is not None}


def _remote(host: str, argv: list[str], timeout: int) -> str:
    """Run argv on a system's host. The remote side gets ONE shell-quoted string that
    parses back into exactly argv — a tab in docker's --format, or a %{...} in curl's
    -w, would otherwise be re-split or reinterpreted by the remote shell."""
    prefix, env, ssh, _ = _login(host)
    remote = shlex.join(["timeout", str(timeout), *argv])
    return subprocess.run(
        ["timeout", str(timeout + 10), *prefix, *ssh, "--", remote],
        capture_output=True, text=True, timeout=timeout + 15, env=env,
    ).stdout


_MAX_CONFIG_BYTES = 1_000_000


def read_config(system: str) -> str:
    """The system's gotcha config, read from its host. The path comes from the inventory,
    which validated it; `~/` is expanded by the remote shell, and only that part."""
    spec = inventory.systems()[system]
    if not spec.get("host") or not spec.get("config"):
        raise KeyError(f"system {system!r} needs both host and config set to read it")
    path = spec["config"]
    target = ('"$HOME"/' + shlex.quote(path[2:])) if path.startswith("~/") \
        else shlex.quote(path)
    prefix, env, ssh, _ = _login(spec["host"])
    r = subprocess.run(["timeout", "25", *prefix, *ssh, "--", f"head -c {_MAX_CONFIG_BYTES} -- {target}"],
                       capture_output=True, text=True, timeout=30, env=env)
    if r.returncode != 0:
        raise OSError((r.stderr or f"exit {r.returncode}").strip()[-300:])
    return r.stdout


# ---------------------------------------------------------------------------
# Targeted execution. The only code that calls inventory.credentials().
# ---------------------------------------------------------------------------

# Not preceded by '%': curl's -w templates contain %{http_code}, which is curl's
# placeholder, not ours. Without the lookbehind, filling a curl command raises.
_PLACEHOLDER = re.compile(r"(?<!%)\{([a-z_]+)\}")
# Injected by us, not by the command table, so every remote call gets them and no
# table edit can quietly drop them.
_SSH_HARDENING = ("-o", "ConnectTimeout=5", "-o", "StrictHostKeyChecking=accept-new")


def _fill(argv: list[str], values: dict[str, str]) -> list[str]:
    """Substitute {placeholders} from a validated value map. Unknown keys are fatal."""
    def one(m: re.Match) -> str:
        key = m.group(1)
        if key not in values:
            raise KeyError(f"unknown placeholder {{{key}}}; known: {sorted(values)}")
        return values[key]
    return [_PLACEHOLDER.sub(one, a) for a in argv]


def run_on(key: str, node: str, timeout: int = 15) -> str:
    """Run an allowlisted command against one system, addressed by LOGICAL NAME.

    The caller passes a name, never a host. Credentials are resolved here, used for the
    duration of this call, and never returned — nothing a caller receives back contains
    a secret, so nothing a caller stores can leak one into graph state.
    """
    table = allowed()
    if key not in table:
        raise KeyError(f"command {key!r} is not allowlisted: {sorted(table)}")
    if MODE == "mock":
        path = FIXTURE_DIR / FIXTURES.get(key, f"{key}.txt")
        if not path.exists():
            raise FileNotFoundError(f"no fixture for {key!r} at {path}")
        return path.read_text()

    rec = inventory.require(node)                # refuses a name not in the inventory
    via = where()
    if via and node != via and os.path.basename(table[key][0]) != "ssh":
        # An HTTP probe of a camera has to leave from the site, where the camera's LAN
        # is. Fill it against the unit, then run it on the host.
        host = str((rec.get("endpoint") or {}).get("host") or "")
        if not host:
            raise KeyError(f"{node!r} has no address in the inventory; nothing to probe")
        if host != "localhost":
            ipaddress.ip_address(host)
        return _remote(via, _fill(list(table[key]), {"host": host}), timeout)
    host = str((rec.get("endpoint") or {}).get("host") or "")
    if not host:
        raise KeyError(f"{node!r} has no address in the inventory; nothing to probe")
    if host != "localhost":
        ipaddress.ip_address(host)               # an address, never a shell-shaped string

    template = list(table[key])
    # Credentials are resolved only for a command that actually logs in. An HTTP probe
    # against a camera with no SSH access block must not be refused for lacking one.
    needs_login = (os.path.basename(template[0]) == "ssh"
                   or any("{ssh_" in a for a in template))
    creds = inventory.credentials(node) if needs_login else None

    values = {"host": host}
    if creds:
        values["ssh_user"] = creds["user"]
        values["ssh_port"] = str(creds["port"])
    argv = _fill(template, values)
    env, prefix = dict(os.environ), []
    if creds and argv and os.path.basename(argv[0]) == "ssh":
        opts = [*_SSH_HARDENING, *_multiplex()]
        if via and node != via:
            # The unit sits on the site's LAN, which this server usually cannot route
            # to. Tunnel through the host — as ProxyCommand rather than -J, because -J
            # does not carry these options to the first hop, and the first hop would
            # then prompt for a host key where nobody can answer.
            jp, _, jssh, jcreds = _login(via)
            if jcreds.get("password"):
                raise RuntimeError(f"reaching {node!r} goes through {via!r}, which needs "
                                   f"key-based SSH — a password cannot be supplied to the "
                                   f"hop and the unit at once")
            opts += ["-o", "ProxyCommand=" + shlex.join([*jssh[:-1], "-W", "%h:%p", jssh[-1]])]
        if creds.get("password"):
            # sshpass reads SSHPASS from the environment. The password never becomes an
            # argv element, so it never appears in `ps`, in a crash dump, or in a log.
            env["SSHPASS"] = creds["password"].reveal()
            prefix = ["sshpass", "-e"]
            opts += ["-o", "BatchMode=no", "-o", "PubkeyAuthentication=no"]
        else:
            opts += ["-o", "BatchMode=yes"]      # fail rather than hang on a prompt
            if creds.get("key_file"):
                opts += ["-i", creds["key_file"]]
        argv = [argv[0], *opts, *argv[1:]]

    # timeout outermost so it bounds the whole call; sshpass immediately inside it so
    # it is ssh's direct parent and owns the pty ssh reads the password from.
    return subprocess.run(
        ["timeout", str(timeout), *prefix, *argv],
        capture_output=True, text=True, timeout=timeout + 5, env=env,
    ).stdout
