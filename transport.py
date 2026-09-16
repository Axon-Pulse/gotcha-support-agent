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
import subprocess
from pathlib import Path

import config_store
import inventory

DEFAULT_ALLOWED: dict[str, tuple[str, ...]] = {
    "topology": ("ecal_mon_cli", "-l"),
    "health":   ("ecal_mon_cli", "--proto", "/system/health", "-c", "40"),
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

MODE = os.environ.get("AGENT_MODE", "mock")
FIXTURE_DIR = Path(os.environ.get("FIXTURE_DIR", "tests/fixtures"))


def run(key: str, timeout: int = 10) -> str:
    """Run an allowlisted command (live) or read its fixture (mock)."""
    table = allowed()
    if key not in table:
        raise KeyError(f"command {key!r} is not allowlisted: {sorted(table)}")
    if MODE == "mock":
        path = FIXTURE_DIR / FIXTURES.get(key, f"{key}.txt")
        if not path.exists():
            raise FileNotFoundError(f"no fixture for {key!r} at {path}")
        return path.read_text()
    return subprocess.run(
        ["timeout", str(timeout), *table[key]],
        capture_output=True, text=True, timeout=timeout + 5,
    ).stdout


def run_argv(argv: list[str], timeout: int = 10) -> str:
    """Run a validated argv list. Callers MUST have validated every element.

    Used only by net.py, whose arguments are parsed as ipaddress/int before arrival.
    """
    if not isinstance(argv, list) or not all(isinstance(a, str) for a in argv):
        raise TypeError("argv must be a list of str")
    return subprocess.run(
        ["timeout", str(timeout), *argv],
        capture_output=True, text=True, timeout=timeout + 5,
    ).stdout


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
        opts = list(_SSH_HARDENING)
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
