"""Files an operator attaches to a question: screenshots, PDFs, logs.

A picture reaches the agents as TEXT. It is described once, on upload, and the
description travels with the question like anything the operator typed. The
alternative — the image itself in every agent's messages — pays for it once per agent
and parks the base64 in every checkpoint in graph.db, for detail the agents that read
node health and process tables rarely need. The original is kept on disk, so a person
can always check the description against what was actually on screen.

Text files skip the model entirely: their content is the description.
"""
from __future__ import annotations

import base64
import binascii
import json
import re
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DIR = ROOT / "attachments"

MAX_BYTES = 10 * 1024 * 1024
# What goes into the question from a text file. Longer files are cut, and the cut is
# SAID in the text the agents read — never silently.
MAX_TEXT_CHARS = 20_000

IMAGE_TYPES = {"image/png", "image/jpeg", "image/gif", "image/webp"}
PDF_TYPE = "application/pdf"
TEXT_EXT = {".txt", ".log", ".json", ".yaml", ".yml", ".md", ".csv", ".xml", ".ini",
            ".conf", ".cfg", ".toml", ".out", ".err"}

_ID = re.compile(r"^[0-9a-f]{12}$")


class AttachmentError(ValueError):
    """Shown to the operator as-is."""


def _kind(name: str, media_type: str) -> str:
    if media_type in IMAGE_TYPES:
        return "image"
    if media_type == PDF_TYPE:
        return "pdf"
    if media_type.startswith("text/") or Path(name).suffix.lower() in TEXT_EXT \
            or media_type in ("application/json", "application/x-yaml", "application/xml"):
        return "text"
    raise AttachmentError(
        f"{name}: {media_type or 'unknown type'} is not supported — attach a screenshot "
        f"(PNG, JPEG, GIF, WebP), a PDF, or a text file such as a log")


def _safe_name(name: str) -> str:
    base = Path(name or "file").name
    return re.sub(r"[^A-Za-z0-9._-]+", "_", base).strip("._") or "file"


def save(name: str, media_type: str, data_b64: str, describe=None) -> dict:
    """Store one upload and turn it into text. Returns its public record.

    `describe(bytes, media_type, name) -> (text, usage)` is injected so tests do not
    need the API; the console passes llm.describe_attachment.
    """
    try:
        raw = base64.b64decode(data_b64 or "", validate=True)
    except (binascii.Error, ValueError) as e:
        raise AttachmentError(f"{name}: the upload was not valid base64") from e
    if not raw:
        raise AttachmentError(f"{name}: the file is empty")
    if len(raw) > MAX_BYTES:
        raise AttachmentError(f"{name}: {len(raw) / 1048576:.1f} MB is over the "
                              f"{MAX_BYTES // 1048576} MB limit")
    media_type = (media_type or "").split(";")[0].strip().lower()
    kind = _kind(name, media_type)

    usage = {"input": 0, "output": 0, "cache_read": 0}
    truncated = False
    if kind == "text":
        text = raw.decode("utf-8", errors="replace")
        full = len(text)
        if full > MAX_TEXT_CHARS:
            text, truncated = text[:MAX_TEXT_CHARS], True
            text += (f"\n[… cut: the first {MAX_TEXT_CHARS:,} of {full:,} characters "
                     f"are shown]")
    else:
        if describe is None:
            raise AttachmentError("no describer configured for images and PDFs")
        text, usage = describe(raw, media_type, name)
        if not text.strip():
            raise AttachmentError(f"{name}: the model returned no description")

    aid = uuid.uuid4().hex[:12]
    d = DIR / aid
    d.mkdir(parents=True, exist_ok=True)
    fname = _safe_name(name)
    (d / fname).write_bytes(raw)
    rec = {"id": aid, "name": name, "file": fname, "media_type": media_type, "kind": kind,
           "bytes": len(raw), "text": text.strip(), "truncated": truncated,
           "usage": usage, "created_at": time.time()}
    (d / "meta.json").write_text(json.dumps(rec, indent=1) + "\n")
    return rec


def load(aid: str) -> dict:
    if not _ID.match(aid or ""):
        raise AttachmentError(f"{aid!r} is not an attachment id")
    p = DIR / aid / "meta.json"
    if not p.exists():
        raise AttachmentError(f"attachment {aid} does not exist (it may have been removed)")
    return json.loads(p.read_text())


def file_path(aid: str) -> Path:
    rec = load(aid)
    return DIR / aid / rec["file"]


def load_many(ids: list[str] | None) -> list[dict]:
    return [load(a) for a in (ids or [])]


_LABEL = {"image": "screenshot / image", "pdf": "PDF", "text": "file"}


def compose(message: str, recs: list[dict]) -> str:
    """The question as the agents see it: what was typed, then each attachment as text."""
    parts = [message.strip()]
    for r in recs:
        how = ("described from the image — the agents did not see the picture itself"
               if r["kind"] != "text" else "contents")
        parts.append(f"[Attached {_LABEL[r['kind']]}: {r['name']} — {how}]\n{r['text']}")
    return "\n\n".join(p for p in parts if p)


def public(recs: list[dict]) -> list[dict]:
    """What a trace keeps: enough to show and re-open each one."""
    return [{k: r[k] for k in ("id", "name", "kind", "media_type", "bytes", "text",
                               "truncated", "usage")} for r in recs]


def usage_of(recs: list[dict]) -> dict:
    out = {"input": 0, "output": 0, "cache_read": 0}
    for r in recs:
        for k in out:
            out[k] += int((r.get("usage") or {}).get(k) or 0)
    return out
