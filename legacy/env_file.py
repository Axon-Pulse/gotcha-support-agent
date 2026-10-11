"""Read .env into the process environment, because nothing else was doing it.

The README has always said `cp .env.example .env`, but the SDK reads os.environ and no
module here ever loaded the file — so a key written where the docs said to put it
authenticated nothing, and the console reported "no credentials resolve" at a person who
had just configured one.

Deliberately not python-dotenv: this is fifteen lines, secrets_store.py already hand-rolls
the same parse for secrets.local.env, and a dependency whose job is one regex is a
dependency you maintain for no reason.

AN EXPORTED VARIABLE ALWAYS WINS. load() never overwrites a name already in os.environ,
which is the same precedence secrets_store.get() uses: a file is what you fall back to,
never what overrides the shell, a systemd unit, or a container's env. Without that rule a
stale .env would silently outrank the key an operator just exported to test with.

ORDERING IS LOAD-BEARING. transport.py fixes MODE at import and slack_app.py freezes its
tokens into module constants, so load() has to run before those imports, not inside main().
The console is the one place that deliberately overrides afterwards — it forces
AGENT_MODE=mock on the line after, so a .env cannot talk it into running live.
"""
import os
from pathlib import Path

from secrets_store import NAME_RX

ROOT = Path(__file__).resolve().parent
# ENV_FILE mirrors SECRETS_FILE in secrets_store: the location is overridable so a test
# — or a deployment that keeps config outside the checkout — can point it elsewhere.
PATH = Path(os.environ.get("ENV_FILE", ROOT / ".env"))


def parse(text: str) -> dict[str, str]:
    """NAME -> value. Unparseable lines are skipped, never raised.

    A malformed .env must not stop the program from starting: the variable it failed to
    set is missing, and the credential check downstream already reports missing far more
    usefully than a traceback at import time would.

    An inline `#` is part of the value, not a comment. Stripping it would quietly corrupt
    any secret containing one, and a whole-line comment is the only kind anyone writes.
    """
    out: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, _, value = line.partition("=")
        name = name.strip()
        if not NAME_RX.match(name):
            continue
        value = value.strip()
        # Quotes are a way to keep leading or trailing spaces, so strip them as a pair.
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        out[name] = value
    return out


def load(path: Path | None = None) -> dict[str, str]:
    """Apply .env to os.environ without overriding anything. Returns what it set.

    A missing file is not an error — it means nothing is configured yet, which is the
    state every fresh clone starts in.
    """
    path = path or PATH
    if not path.exists():
        return {}
    applied = {}
    for name, value in parse(path.read_text()).items():
        if name not in os.environ:
            os.environ[name] = value
            applied[name] = value
    return applied
