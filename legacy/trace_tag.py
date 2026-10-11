"""Human-readable tags for traces: the overall session number, plus the facts beside it.

The trace file keeps its opaque session id. That id is the checkpoint thread, the
bridge ticket and the file name, and it has to exist before the run knows which system
it is looking at or how many tokens it will spend. The tag is derived from what the
trace recorded, so it can say both — and an unknown field is "-", not a guess.

The numbers are the fields that cannot be re-derived: "session 42", or "the 7th gotcha3
session", only stay true if handed out once and remembered. traces/.tags.json holds
both. The tag is the overall number; everything else is reported beside it.
"""
from __future__ import annotations

import json
import re
import threading
import time
from pathlib import Path

import inventory
import trace_summary

UNKNOWN = "-"
_lock = threading.Lock()


def _entries(p: Path) -> list[dict]:
    out = []
    for line in p.read_text().splitlines():
        try:
            e = json.loads(line) if line.strip() else None
        except json.JSONDecodeError:
            continue
        if isinstance(e, dict):
            out.append(e)
    return out


def _started(entries: list[dict], p: Path) -> float:
    for e in entries:
        ts = e.get("started_at") or (e.get("origin") or {}).get("ts")
        if ts:
            return float(ts)
    # Single-run traces are written when the run ends, so mtime is that day.
    return p.stat().st_mtime


def _tokens(entries: list[dict]) -> int | None:
    """input + output + cache read — every token the session made the model process."""
    last = entries[-1] if entries else {}
    if isinstance(last.get("totals"), dict):            # conversation: running total
        rows = [last["totals"]]
    else:
        rows = [e["usage"] for e in entries if isinstance(e.get("usage"), dict)]
        if not rows:
            rows = [t["usage"] for e in entries for t in e.get("transcript") or []
                    if isinstance(t, dict) and isinstance(t.get("usage"), dict)]
    if not rows:
        return None                                     # never recorded, not zero
    return sum(int(r.get(k) or 0) for r in rows for k in ("input", "output", "cache_read"))


def _asker(entries: list[dict]) -> str | None:
    for e in entries:
        o = e.get("origin") or {}
        # Bridge tickets say by="console": that is the channel, not a person.
        who = o.get("user") or (o.get("by") if o.get("by") not in (None, "console") else None)
        if who:
            return str(who)
    return None


def _names(obj, out: set[str]) -> None:
    """Component names from structured fields only. Free text is not scanned for them:
    components can be called "1" or "11", which would match any number in a report."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k in ("node", "name", "target", "component") and isinstance(v, str):
                out.add(v)
            elif k.endswith("_targets") and isinstance(v, list):
                out.update(x for x in v if isinstance(x, str))
            else:
                _names(v, out)
    elif isinstance(obj, list):
        for v in obj:
            _names(v, out)


def _system(entries: list[dict]) -> str | None:
    """The one system this session touched. Two systems, or none, is unknown."""
    # A session that chose its system says so; that beats any inference below.
    for e in entries:
        if chosen := e.get("system") or (e.get("origin") or {}).get("system"):
            return str(chosen)
    try:
        inv = inventory.load()
        systems = set(inventory._systems_doc().get("systems") or {})
    except Exception:                                   # noqa: BLE001 - tag, not a run
        return None
    hit: set[str] = set()
    comps: set[str] = set()
    for e in entries:
        _names([e.get("findings"), e.get("context"), e.get("blocked")], comps)
        text = " ".join(str(e.get(k) or "") for k in ("question", "report"))
        text += " " + str((e.get("origin") or {}).get("question") or "")
        hit.update(s for s in systems
                   if re.search(rf"(?<![\w-]){re.escape(s)}(?![\w-])", text, re.I))
    hit.update(inv[c]["system"] for c in comps if c in inv and inv[c].get("system"))
    return hit.pop() if len(hit) == 1 else None


def _site(system: str | None) -> str | None:
    if not system:
        return None
    try:
        return (inventory._systems_doc().get("systems") or {}).get(system, {}).get("site")
    except Exception:                                   # noqa: BLE001
        return None


def describe(traces_dir: Path) -> list[dict]:
    """Every trace with its tag and parts, newest first. Assigns serials to new traces."""
    idx_path = traces_dir / ".tags.json"
    rows = []
    for p in traces_dir.glob("*.jsonl"):
        entries = _entries(p)
        system = _system(entries)
        sm = trace_summary.summarize(entries)
        rows.append({"status": sm["status"], "status_label": sm["status_label"],
                     "finished": sm["finished"],
                     "problem": sm["problem"], "diagnosis": sm["diagnosis"],
                     "found": sm["found"], "via": sm["via"], "id": p.stem, "bytes": p.stat().st_size, "mtime": p.stat().st_mtime,
                     "started": _started(entries, p), "system": system,
                     "location": _site(system), "tokens": _tokens(entries),
                     "asker": _asker(entries)})

    with _lock:
        try:
            idx = json.loads(idx_path.read_text())
        except (FileNotFoundError, json.JSONDecodeError):
            idx = {}
        # "sessions": overall number, every session gets one, known system or not.
        # "serials": number within its system, given once that system is known.
        for k in ("sessions", "serials", "next"):
            idx.setdefault(k, {})
        idx.setdefault("next_session", 0)
        changed = False
        # Oldest first, so both numbers follow the order the sessions actually happened.
        for r in sorted(rows, key=lambda r: r["started"]):
            if r["id"] not in idx["sessions"]:
                idx["next_session"] += 1
                idx["sessions"][r["id"]] = idx["next_session"]
                changed = True
            if r["id"] in idx["serials"] or not r["system"]:
                continue
            n = idx["next"].get(r["system"], 0) + 1
            idx["next"][r["system"]] = n
            idx["serials"][r["id"]] = {"system": r["system"], "serial": n}
            changed = True
        if changed:
            idx_path.write_text(json.dumps(idx, indent=1) + "\n")

    for r in rows:
        r["session"] = idx["sessions"][r["id"]]
        s = idx["serials"].get(r["id"])
        if s:                   # the system it was numbered under wins over a re-read
            r["system"], r["system_serial"] = s["system"], s["serial"]
            r["location"] = _site(s["system"])
        else:
            r["system_serial"] = None
        r["date"] = time.strftime("%Y-%m-%d", time.localtime(r["started"]))
        # The tag is the overall number alone; the other fields are columns beside it.
        r["tag"] = f"{r['session']:04d}"
    return sorted(rows, key=lambda r: -r["started"])
