"""The console's own file handling, shared by everything that keeps state or secrets on disk.

- write_private: replace a file atomically, owner-only from the first byte.
- file_lock: one writer at a time for a state file, across threads and processes (the server
  and `python -m console.auth` both change the same files).
- env files (NAME=value lines): read entries, and change some of them in place, keeping every
  other line, comment and order as it was.
- "who changed what, when" metadata, kept beside a file and never holding a value.
"""
import fcntl
import json
import os
import re
import secrets
import threading
import time
from contextlib import contextmanager
from pathlib import Path

from console.safe_text import lines
from console.settings import S

# A NAME=value line, as both docker compose and the shell read it: any variable-like name.
ENV_LINE_RX = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)=(.*)$")


def write_private(path: Path, text: str) -> None:
    """Replace `path` with `text`, owner-only from the first byte, never half-written."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{secrets.token_hex(4)}.tmp")
    fd = os.open(tmp, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


_THREAD_LOCKS: dict[str, threading.Lock] = {}
_GUARD = threading.Lock()


@contextmanager
def file_lock(name: str):
    """Hold `console/state/.<name>.lock` exclusively: a thread lock for this process, and a
    flock for the others."""
    with _GUARD:
        tlock = _THREAD_LOCKS.setdefault(name, threading.Lock())
    S.state_dir.mkdir(parents=True, exist_ok=True)
    with tlock:
        fd = os.open(S.state_dir / f".{name}.lock", os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            os.close(fd)  # closing releases the flock


# --------------------------------------------------------------------------
# Env files
# --------------------------------------------------------------------------

def env_entries(path: Path) -> list[tuple[str, str]]:
    """(NAME, raw value) for each NAME=value line, in file order; comments skipped."""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return []
    return [(m.group(1), m.group(2)) for m in map(ENV_LINE_RX.match, lines(text)) if m]


def env_update(path: Path, changes: dict[str, str | None], header: str = "") -> None:
    """Change some entries of an env file in place. `changes` maps a NAME to its whole new line
    (written where the old one was, or appended), or to None to remove it. Every other line
    stays as it was; a later duplicate of a changed name, which would win when read, goes.
    `header` starts a file that doesn't exist yet."""
    if not changes:
        return
    try:
        rows = lines(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        rows = header.rstrip("\n").split("\n") if header else []
    out, done = [], set()
    for line in rows:
        m = ENV_LINE_RX.match(line)
        name = m.group(1) if m else None
        if name in changes:
            if changes[name] is not None and name not in done:
                out.append(changes[name])
            done.add(name)
            continue
        out.append(line)
    out += [line for name, line in changes.items() if line is not None and name not in done]
    write_private(path, "\n".join(out) + "\n")


# --------------------------------------------------------------------------
# Who changed what, when
# --------------------------------------------------------------------------

def meta_load(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        return {}


def meta_touch(meta: dict, key: str, who: str) -> None:
    meta[key] = {"at": round(time.time(), 3), "by": who}


def meta_save(path: Path, meta: dict) -> None:
    write_private(path, json.dumps(meta, indent=1, sort_keys=True))
