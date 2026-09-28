"""The console <-> chat bridge.

The console writes a ticket to bridge/requests/, and a report comes back in
bridge/reports/. Both sides are plain JSON files on disk, which is the whole design:

    console UI ──POST──> bridge/requests/<id>.json   (pending)
                              │
                     a human types "go" in the Claude Code chat
                              │
                              ▼
              read the KB, run read-only checks, analyse
                              │
                    bridge/reports/<id>.json   (done)
                              │
    console UI <──poll GET──  renders it with the same report card the real
                              pipeline uses

WHY A HUMAN TRIGGER. There is no daemon on the chat side — nothing polls this
directory. The assistant acts only when someone sends it a message, so the queue is
drained on request rather than on arrival. A file queue is honest about that: the
work is visibly outstanding until somebody picks it up.

WHAT THIS IS NOT. It bypasses LangGraph, the agents, the supervisor and the tool
registry entirely. It exercises the DOCUMENTATION and the HARDWARE, not the pipeline.
The Run tab is the other half: it exercises the pipeline against recorded fixtures and
never touches the bench.

The report is held to graph.REPORT_SCHEMA's required fields so the console can render
it with the same card, and so a diagnosis cannot quietly skip `unknowns` or `escalate`.
"""
import json
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent
REQUESTS = ROOT / "bridge" / "requests"
REPORTS = ROOT / "bridge" / "reports"
TRACES = ROOT / "traces"

STATUSES = ("pending", "in_progress", "done", "failed")


class BridgeError(ValueError):
    """A malformed ticket or report. The message is shown to the operator."""


def _dirs() -> None:
    REQUESTS.mkdir(parents=True, exist_ok=True)
    REPORTS.mkdir(parents=True, exist_ok=True)


def _read(p: Path) -> dict:
    """Missing or half-written is empty, not an error: the reader polls while the
    writer is still working, so both states are normal."""
    try:
        return json.loads(p.read_text() or "{}")
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


# ---------------------------------------------------------------- console side

def submit(question: str, by: str = "console", typed: str | None = None,
           attachments: list[dict] | None = None) -> str:
    """Queue a ticket. Returns the session id the console then polls.

    `question` is what the chat side reads, attachment descriptions included. `typed`
    and `attachments` keep what the operator typed and what they attached apart, so the
    trace can show each as itself.
    """
    q = (question or "").strip()
    if not q:
        raise BridgeError("a ticket needs a description of what the system is doing")
    _dirs()
    sid = uuid.uuid4().hex[:12]
    now = time.time()
    (REQUESTS / f"{sid}.json").write_text(json.dumps({
        "id": sid, "question": q, "by": by,
        **({"typed": typed, "attachments": attachments} if attachments else {}),
        "submitted_at": now, "status": "pending",
    }, indent=2) + "\n")
    # In the trace from the moment it is asked, so a ticket nobody ever picks up is
    # still visible — and the trace dates from the question, not from the answer.
    _trace_line(sid, status="pending", started_at=now,
                origin={"via": "bridge", "by": by,
                        "question": typed if attachments else q, "ts": now},
                **({"attachments": attachments} if attachments else {}))
    return sid


def _trace_line(sid: str, **fields) -> None:
    TRACES.mkdir(exist_ok=True)
    with (TRACES / f"{sid}.jsonl").open("a") as f:
        f.write(json.dumps(fields, default=str) + "\n")


def status(sid: str) -> dict | None:
    """Current state of one session: the request, plus the report once it exists."""
    req = _read(REQUESTS / f"{sid}.json")
    if not req:
        return None
    rep = _read(REPORTS / f"{sid}.json")
    return {**req, **rep} if rep else req


def queue() -> list[dict]:
    """Every session, newest first."""
    _dirs()
    out = [status(p.stem) for p in REQUESTS.glob("*.json")]
    return sorted([s for s in out if s], key=lambda s: -s.get("submitted_at", 0))


# ---------------------------------------------------------------- chat side

def pending() -> list[dict]:
    """Tickets waiting to be picked up, oldest first — the work queue."""
    return sorted([s for s in queue() if s.get("status") == "pending"],
                  key=lambda s: s.get("submitted_at", 0))


def claim(sid: str) -> dict:
    """Mark a ticket in progress so the console stops showing it as untouched."""
    p = REQUESTS / f"{sid}.json"
    req = _read(p)
    if not req:
        raise BridgeError(f"no ticket {sid!r}")
    req["status"] = "in_progress"
    req["claimed_at"] = time.time()
    p.write_text(json.dumps(req, indent=2) + "\n")
    _trace_line(sid, status="in_progress")
    return req


def validate_report(report: dict) -> None:
    """Hold the report to the same shape the real pipeline must produce.

    Not pedantry: `unknowns` and `escalate` are the fields a confident wrong answer
    leaves out, and they are the ones worth being forced to fill in.
    """
    from graph import REPORT_SCHEMA
    missing = [k for k in REPORT_SCHEMA["required"] if k not in report]
    if missing:
        raise BridgeError(f"report is missing required fields: {missing}")
    if report["confidence"] not in ("low", "medium", "high"):
        raise BridgeError("confidence must be low, medium or high")
    for k in ("evidence", "suggested_actions", "unknowns"):
        if not isinstance(report[k], list):
            raise BridgeError(f"{k} must be a list")
    if not isinstance(report["escalate"], bool):
        raise BridgeError("escalate must be true or false")


def publish(sid: str, report: dict, commands: list[dict] | None = None,
            notes: str = "", kb_gaps: list[str] | None = None,
            status_: str = "done") -> dict:
    """Write the report, and a trace beside every other surface's.

    `kb_gaps` is the point of the exercise: every fact used that was not in
    system-model.md or a recorded case is a documentation gap, recorded here rather
    than left in a chat transcript.
    """
    if status_ not in ("done", "failed"):
        raise BridgeError("a published session is done or failed")
    req = _read(REQUESTS / f"{sid}.json")
    if not req:
        raise BridgeError(f"no ticket {sid!r}")
    if status_ == "done":
        validate_report(report)

    out = {"status": status_, "report": report, "commands": commands or [],
           "notes": notes, "kb_gaps": kb_gaps or [], "published_at": time.time()}
    _dirs()
    (REPORTS / f"{sid}.json").write_text(json.dumps(out, indent=2) + "\n")

    p = REQUESTS / f"{sid}.json"
    p.write_text(json.dumps({**req, "status": status_}, indent=2) + "\n")

    TRACES.mkdir(exist_ok=True)
    with (TRACES / f"{sid}.jsonl").open("a") as f:
        f.write(json.dumps({
            "status": status_,
            "origin": {"via": "bridge", "by": req.get("by"),
                       "question": req.get("typed") or req.get("question"),
                       "ts": time.time()},
            "report": report, "commands": commands or [],
            "kb_gaps": kb_gaps or [], "notes": notes,
        }, default=str) + "\n")
    return {**req, **out}
