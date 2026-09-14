"""Runbook search over kb/*.md — plain lexical scoring, no embeddings.

The corpus is ~1.3k lines and the vocabulary is exact-match friendly
(PROC_EXITED_ERROR, ecal_shutdown_wait_ms, tailscale0), which is where lexical
search wins. Revisit at ~50 documents.

Returns excerpts, never whole documents: a full-document fetch is how a 328-line
guide ends up in context.
"""
import re
from pathlib import Path

from registry import tool

KB_DIR = Path(__file__).resolve().parent.parent / "kb"
_WORD = re.compile(r"[a-z0-9_/.-]{3,}")


def _tokens(s: str) -> list[str]:
    return _WORD.findall(s.lower())


@tool({
    "name": "search_runbook",
    "description": (
        "Search the local runbook for a symptom, error string or topic name. Use exact "
        "strings from logs or tool output where you have them — the index is lexical."
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
    for path in sorted(KB_DIR.glob("*.md")):
        text = path.read_text()
        toks = _tokens(text)
        if not toks:
            continue
        counts = {t: toks.count(t) for t in q}
        score = sum(v for v in counts.values())
        if not score:
            continue
        best = max(q, key=lambda t: counts[t])
        i = text.lower().find(best)
        start = max(0, text.rfind("\n\n", 0, i))
        hits.append({"doc": path.stem, "score": score,
                     "excerpt": text[start:start + 800].strip()})
    hits.sort(key=lambda h: -h["score"])
    return {"hits": hits[:top_k], "docs_searched": len(list(KB_DIR.glob("*.md")))}
