"""Search over recorded cases in kb/cases/ — plain lexical scoring, no embeddings.

This searches the case history ONLY. The system model is already in the cached system
prompt, so searching it would spend a turn re-fetching something the model can already
see. Cases are the half that is not in the prompt, which is what makes this tool
load-bearing: it is the only way an agent reaches an incident somebody recorded earlier.

One file per case, so scoring ranks individual incidents. A single growing file would
score as one document and return the same hit for every query.

The vocabulary is exact-match friendly (PROC_EXITED_ERROR, ecal_shutdown_wait_ms,
tailscale0), which is where lexical search wins. Revisit at ~50 cases.

Returns excerpts, never whole documents: a full-document fetch is how a long guide ends
up in context.
"""
import re
from pathlib import Path

from registry import tool

KB_DIR = Path(__file__).resolve().parent.parent / "kb" / "cases"
_WORD = re.compile(r"[a-z0-9_/.-]{3,}")
# A token worth matching on its own: an identifier (has a separator) or a long word.
# Without this, one incidental everyday word ("ever", "since") scores a hit, and a
# no-match query — the signal that this fault is NEW — silently becomes a weak match.
_DISTINCTIVE = re.compile(r"[_/.-]|^.{8,}$")


def _tokens(s: str) -> list[str]:
    return _WORD.findall(s.lower())


@tool({
    "name": "search_runbook",
    "description": (
        "Search RECORDED CASES — faults that were diagnosed here before — by symptom, "
        "error string or topic name. Use exact strings from logs or tool output where you "
        "have them — a match needs at least one long or identifier-shaped word, so "
        "everyday words alone will not find anything. It does NOT search the system "
        "model, which is "
        "already in your context. No hits means this pattern has not been seen before: "
        "say so and reason from the system model rather than forcing a match."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Symptom or literal error text."},
            "top_k": {"type": "integer", "minimum": 1, "maximum": 5, "default": 3},
        },
        "required": ["query"], "additionalProperties": False,
    },
})
def search_runbook(query: str, top_k: int = 3) -> dict:
    q = set(_tokens(query))
    if not q:
        return {"hits": []}
    hits = []
    if not KB_DIR.exists():
        return {"hits": [], "cases_searched": 0}
    for path in sorted(KB_DIR.glob("*.md")):
        text = path.read_text()
        toks = _tokens(text)
        if not toks:
            continue
        counts = {t: toks.count(t) for t in q}
        score = sum(v for v in counts.values())
        if not score:
            continue
        # A hit needs at least one DISTINCTIVE token. Everyday words ("ever", "before")
        # add to the score but cannot create a match on their own, or a query about
        # something genuinely new silently recalls an unrelated case.
        if not any(_DISTINCTIVE.search(t) for t in q if counts[t]):
            continue
        best = max(q, key=lambda t: counts[t])
        i = text.lower().find(best)
        start = max(0, text.rfind("\n\n", 0, i))
        hits.append({"doc": path.stem, "score": score,
                     "excerpt": text[start:start + 800].strip()})
    hits.sort(key=lambda h: -h["score"])
    return {"hits": hits[:top_k], "cases_searched": len(list(KB_DIR.glob("*.md")))}
