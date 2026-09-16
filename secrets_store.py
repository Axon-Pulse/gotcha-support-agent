"""Local secret store. The console writes here; the registry never does.

systems_inventory.yaml may never hold a secret VALUE — inventory.py refuses to load a
file that does, and that refusal is what makes the registry safe to commit. But the
console has to let an admin type a password and see it again later. So the split is:

    systems_inventory.yaml   password_env: MAGOS_SSH_PW      <- git-safe, model-adjacent
    secrets.local.env        MAGOS_SSH_PW=hunter2            <- gitignored, mode 0600

Resolution order for a credential is os.environ FIRST, then this file, so an operator
who exports the variable never has to write the secret to disk at all.

Nothing here is reachable from a tool or from graph state: inventory.credentials() reads
it, transport.py calls that, and the console API reads it to render a masked field for
the human at localhost. The agent-facing view is built from an allowlist that has no
path to any of it.
"""
import os
import re
import stat
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PATH = Path(os.environ.get("SECRETS_FILE", ROOT / "secrets.local.env"))

NAME_RX = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")


class SecretError(ValueError):
    """A rejected secret name or value."""


def _check(name: str) -> str:
    if not NAME_RX.match(name or ""):
        raise SecretError(
            f"{name!r} is not a valid environment variable name "
            f"(A-Z, digits and underscore, starting with a letter)")
    return name


def load() -> dict[str, str]:
    """NAME -> value. Missing file is not an error; nothing is configured yet."""
    if not PATH.exists():
        return {}
    out: dict[str, str] = {}
    for line in PATH.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, _, value = line.partition("=")
        if NAME_RX.match(name.strip()):
            out[name.strip()] = value
    return out


def _write(data: dict[str, str]) -> None:
    body = "".join(f"{k}={v}\n" for k, v in sorted(data.items()))
    PATH.write_text(
        "# Written by the support-agent console. Values are secrets: do not commit.\n"
        "# The registry references these by NAME (password_env:), never by value.\n" + body)
    # Owner-only. Best effort: a filesystem without POSIX modes is not a reason to fail.
    try:
        PATH.chmod(stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        pass


def get(name: str) -> str | None:
    """Environment wins, so an exported variable is never shadowed by a stale file."""
    return os.environ.get(name) or load().get(name)


def is_in_environment(name: str) -> bool:
    return bool(os.environ.get(name))


def put(name: str, value: str) -> None:
    _check(name)
    if not value:
        raise SecretError("refusing to store an empty secret; delete it instead")
    data = load()
    data[name] = value
    _write(data)


def delete(name: str) -> bool:
    data = load()
    if name not in data:
        return False
    del data[name]
    _write(data)
    return True
