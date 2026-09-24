#!/usr/bin/env python3
"""CLI. Drives the graph and handles approval prompts.

    python run.py "the acoustic sensor shows no tracks"
    AGENT_MODE=live python run.py "..."        # real commands instead of fixtures
    python run.py --resume <session_id>        # pick up a pending approval
"""
import argparse
import json
import logging
import os
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

import env_file

# Before transport, which fixes MODE at import — and before the SDK client is built.
env_file.load()

from langgraph.checkpoint.sqlite import SqliteSaver  # noqa: E402
from langgraph.types import Command  # noqa: E402

import llm  # noqa: E402
import transport  # noqa: E402
from graph import build  # noqa: E402
from registry import load_tools  # noqa: E402

ROOT = Path(__file__).resolve().parent
TRACES = ROOT / "traces"


def _edit(text: str) -> str:
    with tempfile.NamedTemporaryFile("w+", suffix=".md", delete=False) as f:
        f.write(text)
        path = f.name
    subprocess.run([os.environ.get("EDITOR", "nano"), path], check=False)
    return Path(path).read_text()


def _trace(session: str, out: dict) -> None:
    TRACES.mkdir(exist_ok=True)
    with (TRACES / f"{session}.jsonl").open("a") as f:
        f.write(json.dumps({"findings": out.get("findings", []),
                            "transcript": out.get("transcript", []),
                            "report": out.get("report"),
                            # Whole-process total, so it includes the routing and
                            # synthesis calls that never reach a transcript entry.
                            "usage": llm.totals()}, default=str) + "\n")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("question", nargs="*")
    ap.add_argument("--resume", metavar="SESSION_ID")
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO if a.verbose else logging.WARNING,
                        format="%(levelname)s %(name)s: %(message)s")

    load_tools()                     # refuses to register any write tool
    llm.reset_usage()                # totals cover this process, not this module's life
    session = a.resume or uuid.uuid4().hex[:12]
    print(f"session {session}  mode={transport.MODE}\n")

    with SqliteSaver.from_conn_string(str(ROOT / "graph.db")) as cp:
        app = build().compile(checkpointer=cp)
        cfg = {"configurable": {"thread_id": session}}

        if a.resume:
            out = app.invoke(None, cfg)
        else:
            if not a.question:
                ap.error("give a question, or --resume a session")
            out = app.invoke({"question": " ".join(a.question), "session_id": session,
                              "findings": [], "visited": [], "transcript": []}, cfg)

        while "__interrupt__" in out:
            req = out["__interrupt__"][0].value
            print("\n" + "=" * 62)
            print("The agent proposes adding this to the knowledge base:")
            print("=" * 62)
            print(req["preview_md"])
            print("=" * 62)
            ans = input("Add it? [y = yes / e = edit first / N = no] ").strip().lower()
            if ans == "e":
                edited = _edit(req["preview_md"])
                sc = dict(req["scenario"])
                sc["_md"] = edited
                out = app.invoke(Command(resume={"approved": True, "edited": sc}), cfg)
            else:
                out = app.invoke(Command(resume={"approved": ans == "y"}), cfg)

        r = out.get("report") or {}
        print("\n" + "-" * 62)
        print("ROOT CAUSE :", r.get("root_cause") or "(not determined)")
        print("CONFIDENCE :", r.get("confidence"))
        if r.get("escalate"):
            print("ESCALATE   : yes —", r.get("escalate_reason", ""))
        for e in r.get("evidence", []):
            print("  evidence :", e)
        for u in r.get("unknowns", []):
            print("  unknown  :", u)
        for s in r.get("suggested_actions", []):
            print("  next     :", s)
        if out.get("saved"):
            print("\nknowledge base updated.")
        _trace(session, out)
        print(f"\ntrace: traces/{session}.jsonl")
    return 0


if __name__ == "__main__":
    sys.exit(main())
