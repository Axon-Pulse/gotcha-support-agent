"""The skill's knowledge base, as files: cases, their SKILL.md index rows, their log signatures.

A case is kb/cases/<slug>.md:

    ---
    topics:
    - radar
    symptoms:
    - the radar is connected but there are no detections
    ---

    # Title

    Intro paragraph(s).

    ## Evidence
    ...

The H2 sections are free: most cases use Evidence / Root cause / Checks (read-only) / Fix /
Verify / If that didn't work, some use others, so the editor treats them as an ordered list
rather than a fixed form. `split` and `join` are exact inverses on every case in the KB (a
test checks); a file they cannot reproduce byte for byte is offered as raw markdown instead,
so the structured editor can never quietly rewrite one.

Two other files must agree with the cases, and scripts/selftest.sh checks that they do:
- SKILL.md §3 has an index table with one row per case, its first cell `` `slug` ``.
- scripts/signatures.txt has at most one line per case, `slug|ERE`, where the ERE is grepped
  case-insensitively over the site's logs and a hit is tagged [slug] in the triage.
"""
import hashlib
import os
import re
import subprocess
import threading
import unicodedata
from pathlib import Path

import yaml

from console.settings import S

SLUG = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
TOPIC = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
SEEDED_SECTIONS = ["Evidence", "Root cause", "Checks (read-only)", "Fix", "Verify",
                   "If that didn't work"]
SIGS_MARKER = "__GOTCHA_SIGS__"        # run_triage.sh's heredoc end marker; see check_regex
_FM = re.compile(r"\A---\n(.*?\n)---\n", re.S)
_FENCE = re.compile(r"^\s*(```|~~~)")
INDEX_HEADER = re.compile(r"^\|\s*case\s*\|", re.I)

# One writer at a time across the three files, so a case and its index row move together.
LOCK = threading.RLock()


class KbError(ValueError):
    """A request the KB refuses, with a message for the person who made it."""


def etag(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def write_atomic(path: Path, text: str) -> None:
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


# --------------------------------------------------------------------------
# Slugs and paths
# --------------------------------------------------------------------------

def slugify(raw: str) -> str:
    """Mirrored by slugify() in index.html; a test runs both over the same inputs."""
    s = unicodedata.normalize("NFKD", raw or "").encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", "-", s).strip("-")[:64].rstrip("-")


def case_path(slug: str, must_exist: bool = True) -> Path:
    if not SLUG.match(slug or ""):
        raise KbError(f"not a case name: {slug!r}")
    p = (S.cases_dir / f"{slug}.md").resolve()
    if p.parent != S.cases_dir.resolve():
        raise KbError(f"not a case name: {slug!r}")
    if must_exist and not p.exists():
        raise FileNotFoundError(slug)
    return p


# --------------------------------------------------------------------------
# A case file <-> its parts
# --------------------------------------------------------------------------

def _fm_render(topics: list[str], symptoms: list[str]) -> str:
    return "---\n" + yaml.safe_dump({"topics": topics, "symptoms": symptoms}, sort_keys=False,
                                    allow_unicode=True, width=10_000) + "---\n"


def _as_list(v) -> list[str]:
    if v is None:
        return []
    if isinstance(v, str):
        return [x.strip() for x in v.splitlines() if x.strip()]
    return [str(x).strip() for x in v if str(x).strip()]


def split(text: str) -> dict:
    """{topics, symptoms, title, intro, sections: [{heading, body}], structured}.

    Headings are only looked for outside ``` fences, since a `# comment` in a code block is
    not a title. Blank lines around a part are dropped and put back by `join`."""
    topics, symptoms, rest = [], [], text
    m = _FM.match(text)
    if m:
        try:
            fm = yaml.safe_load(m.group(1))
        except yaml.YAMLError:
            fm = None
        if isinstance(fm, dict):
            topics, symptoms = _as_list(fm.get("topics")), _as_list(fm.get("symptoms"))
            rest = text[m.end():]
    title, intro, sections, cur, fence = None, [], [], None, False
    for line in rest.split("\n"):
        if _FENCE.match(line):
            fence = not fence
        if not fence and title is None and cur is None and line.startswith("# "):
            title = line[2:].strip()
            continue
        if not fence and line.startswith("## "):
            cur = {"heading": line[3:].strip(), "body": []}
            sections.append(cur)
            continue
        (cur["body"] if cur else intro).append(line)
    parts = {"topics": topics, "symptoms": symptoms, "title": title or "",
             "intro": "\n".join(intro).strip("\n"),
             "sections": [{"heading": s["heading"], "body": "\n".join(s["body"]).strip("\n")}
                          for s in sections]}
    parts["structured"] = join(parts) == text
    return parts


def join(parts: dict) -> str:
    out = [_fm_render(parts.get("topics") or [], parts.get("symptoms") or []).rstrip("\n"), ""]
    out.append(f"# {parts.get('title', '').strip()}")
    if (parts.get("intro") or "").strip("\n"):
        out += ["", parts["intro"].strip("\n")]
    for s in parts.get("sections") or []:
        out += ["", f"## {s['heading'].strip()}"]
        if s.get("body", "").strip("\n"):
            out += ["", s["body"].strip("\n")]
    return "\n".join(out) + "\n"


def validate_parts(parts: dict) -> dict:
    topics = []
    for t in parts.get("topics") or []:
        t = str(t).strip().lower()
        if not TOPIC.match(t):
            raise KbError(f"not a topic name: {t!r} (lowercase letters, digits, - and _)")
        if t not in topics:
            topics.append(t)
    symptoms = [s.strip() for s in parts.get("symptoms") or [] if str(s).strip()]
    if any("\n" in s for s in symptoms):
        raise KbError("a symptom is one line")
    title = (parts.get("title") or "").strip()
    if not title or "\n" in title:
        raise KbError("a case needs a one-line title")
    sections = []
    for s in parts.get("sections") or []:
        h = (s.get("heading") or "").strip()
        if not h or "\n" in h:
            raise KbError("every section needs a one-line heading")
        sections.append({"heading": h, "body": s.get("body") or ""})
    return {"topics": topics, "symptoms": symptoms, "title": title,
            "intro": parts.get("intro") or "", "sections": sections}


def validate_raw(text: str) -> None:
    m = _FM.match(text)
    if not m:
        raise KbError("a case starts with a --- frontmatter block (topics, symptoms)")
    try:
        fm = yaml.safe_load(m.group(1))
    except yaml.YAMLError as e:
        raise KbError(f"the frontmatter is not valid YAML: {e}")
    if not isinstance(fm, dict) or not isinstance(fm.get("topics", []), list) \
            or not isinstance(fm.get("symptoms", []), list):
        raise KbError("the frontmatter needs `topics:` and `symptoms:` as lists")


# --------------------------------------------------------------------------
# SKILL.md §3 index
# --------------------------------------------------------------------------

def _skill_md() -> Path:
    return S.skill_dir / "SKILL.md"


def _index_bounds(lines: list[str]) -> tuple[int, int]:
    """(first row, end) of the case index table: the rows after its header and separator."""
    for i, line in enumerate(lines):
        if INDEX_HEADER.match(line) and i + 1 < len(lines) and lines[i + 1].startswith("|--"):
            end = i + 2
            while end < len(lines) and lines[end].startswith("|"):
                end += 1
            return i + 2, end
    raise KbError("SKILL.md has no case index table (a `| case | ...` header)")


def _row_slug(line: str) -> str | None:
    m = re.match(r"^\|\s*`([^`]+)`\s*\|", line)
    return m.group(1) if m else None


def _cells(line: str) -> list[str]:
    """A row's cells. A cell may hold an escaped \\| or a | inside `code`, so split by hand."""
    cells, cur, code, i, s = [], [], False, 0, line.strip()
    s = s[1:] if s.startswith("|") else s
    while i < len(s):
        c = s[i]
        if c == "\\" and i + 1 < len(s):
            cur.append(s[i:i + 2]); i += 2; continue
        if c == "`":
            code = not code
        if c == "|" and not code:
            cells.append("".join(cur).strip()); cur = []
        else:
            cur.append(c)
        i += 1
    if "".join(cur).strip():
        cells.append("".join(cur).strip())
    return cells


def index_rows() -> dict[str, dict]:
    lines = _skill_md().read_text(encoding="utf-8").split("\n")
    a, b = _index_bounds(lines)
    rows = {}
    for line in lines[a:b]:
        slug = _row_slug(line)
        if slug:
            c = _cells(line)
            rows[slug] = {"says": c[1] if len(c) > 1 else "",
                          "evidence": " | ".join(c[2:]) if len(c) > 2 else ""}
    return rows


def _check_cell(v: str, what: str) -> str:
    v = (v or "").strip()
    if not v:
        raise KbError(f"the index row needs '{what}'")
    if "\n" in v:
        raise KbError(f"'{what}' is one line")
    if "|" in _strip_code_and_escapes(v):
        raise KbError(f"'{what}' can't contain | outside `code` (it ends a table cell); "
                      "write 'or', or escape it as \\|")
    return v


def _strip_code_and_escapes(v: str) -> str:
    return re.sub(r"`[^`]*`", "", v.replace("\\|", ""))


def set_index_row(slug: str, says: str | None, evidence: str | None, *, rename_to: str = "",
                  remove: bool = False) -> None:
    """Edit, add, rename or remove one case's row. Every other line of SKILL.md is untouched."""
    with LOCK:
        p = _skill_md()
        lines = p.read_text(encoding="utf-8").split("\n")
        a, b = _index_bounds(lines)
        at = next((i for i in range(a, b) if _row_slug(lines[i]) == slug), None)
        if remove:
            if at is not None:
                del lines[at]
                write_atomic(p, "\n".join(lines))
            return
        if rename_to and says is None:
            if at is not None:          # a case with no row yet has nothing to rename
                lines[at] = lines[at].replace(f"`{slug}`", f"`{rename_to}`", 1)
                write_atomic(p, "\n".join(lines))
            return
        row = (f"| `{rename_to or slug}` | {_check_cell(says, 'PM usually says')} "
               f"| {_check_cell(evidence, 'decisive triage evidence')} |")
        if at is None:
            lines.insert(b, row)
        elif lines[at] != row:
            lines[at] = row
        else:
            return
        write_atomic(p, "\n".join(lines))


# --------------------------------------------------------------------------
# scripts/signatures.txt
# --------------------------------------------------------------------------

def _sigs() -> Path:
    return S.skill_dir / "scripts" / "signatures.txt"


def split_alternatives(ere: str) -> list[str]:
    """`a|b(c|d)` -> ['a', 'b(c|d)']: split only at a top-level, unescaped |, outside [...]."""
    out, cur, depth, bracket, i = [], [], 0, False, 0
    while i < len(ere):
        c = ere[i]
        if c == "\\" and i + 1 < len(ere):
            cur.append(ere[i:i + 2]); i += 2; continue
        if bracket:
            if c == "]" and cur and cur[-1] not in ("[", "[^"):
                bracket = False
        elif c == "[":
            bracket = True
        elif c == "(":
            depth += 1
        elif c == ")":
            depth = max(0, depth - 1)
        elif c == "|" and depth == 0:
            out.append("".join(cur)); cur = []; i += 1; continue
        cur.append(c)
        i += 1
    out.append("".join(cur))
    return out


def check_regex(ere: str) -> None:
    """A signature goes into a heredoc that runs on the site (run_triage.sh), and every line's
    ERE is joined with | into one grep (triage_remote.sh), so a bad one breaks them all."""
    if not ere or not ere.strip():
        raise KbError("an empty pattern would tag every log line")
    if SIGS_MARKER in ere:
        raise KbError(f"a pattern can't contain {SIGS_MARKER}: it ends the heredoc that "
                      "carries signatures.txt to the site, and the rest would run as shell there")
    if any(ord(c) < 32 or ord(c) == 127 for c in ere):
        raise KbError("a pattern can't contain a newline, tab or other control character")
    r = subprocess.run(["grep", "-Eiq", "-e", ere], input="\n", capture_output=True, text=True)
    if r.returncode == 2:
        raise KbError(f"not a valid extended regex: {ere!r} ({r.stderr.strip()})")
    if r.returncode == 0:
        raise KbError(f"{ere!r} matches an empty line, so it would tag every log line")


def check_signatures_text(text: str) -> None:
    """The whole file, as the Reference docs editor saves it."""
    cases = {p.stem for p in S.cases_dir.glob("*.md")}
    for n, line in enumerate(text.split("\n"), 1):
        if not line.strip() or line.startswith("#"):
            continue
        if SIGS_MARKER in line:
            check_regex(line)
        slug, sep, ere = line.partition("|")
        if not sep:
            raise KbError(f"line {n}: expected <case-slug>|<regex>")
        name = slug[3:] if slug.startswith("ok:") else slug
        if not SLUG.match(name):
            raise KbError(f"line {n}: {slug!r} is not a case slug (or ok:<name>)")
        if not slug.startswith("ok:") and slug not in cases:
            raise KbError(f"line {n}: no case kb/cases/{slug}.md")
        try:
            check_regex(ere)
        except KbError as e:
            raise KbError(f"line {n}: {e}")


def signatures_for(slug: str) -> list[str]:
    for line in _sigs().read_text(encoding="utf-8").split("\n"):
        if line.startswith(f"{slug}|"):
            return split_alternatives(line[len(slug) + 1:])
    return []


def set_signatures(slug: str, patterns: list[str] | None, *, rename_to: str = "") -> None:
    """Replace the case's line (or add one at the end, or remove it when patterns is empty).
    patterns=None with rename_to only renames the line's slug."""
    with LOCK:
        p = _sigs()
        text = p.read_text(encoding="utf-8")
        lines = text.split("\n")
        at = [i for i, line in enumerate(lines) if line.startswith(f"{slug}|")]
        if patterns is None:
            pats = signatures_for(slug)
        else:
            pats = [x.strip() for x in patterns if x and x.strip()]
            for x in pats:
                check_regex(x)
            if pats:
                check_regex("|".join(pats))
        new = f"{rename_to or slug}|{'|'.join(pats)}" if pats else None
        if at:
            lines[at[0]:at[0] + 1] = [new] if new else []
            for i in reversed(at[1:]):
                del lines[i - (0 if new else 1)]
        elif new:
            while lines and lines[-1] == "":
                lines.pop()
            lines += [new, ""]
        out = "\n".join(lines)
        if out != text:
            write_atomic(p, out)


# --------------------------------------------------------------------------
# Cases
# --------------------------------------------------------------------------

def list_cases() -> list[dict]:
    idx = index_rows()
    sigs = {line.split("|", 1)[0] for line in _sigs().read_text(encoding="utf-8").split("\n")
            if "|" in line and not line.startswith("#")}
    out = []
    for p in sorted(S.cases_dir.glob("*.md")):
        parts = split(p.read_text(encoding="utf-8"))
        out.append({"slug": p.stem, "title": parts["title"], "topics": parts["topics"],
                    "symptoms": len(parts["symptoms"]), "bytes": p.stat().st_size,
                    "indexed": p.stem in idx, "signatures": p.stem in sigs,
                    "structured": parts["structured"], "mtime": p.stat().st_mtime})
    return out


def all_topics() -> list[str]:
    ts = set()
    for p in S.cases_dir.glob("*.md"):
        ts.update(split(p.read_text(encoding="utf-8"))["topics"])
    return sorted(ts)


def get_case(slug: str) -> dict:
    p = case_path(slug)
    text = p.read_text(encoding="utf-8")
    parts = split(text)
    return {"slug": slug, **parts, "raw": text, "etag": etag(text),
            "index": index_rows().get(slug), "signatures": signatures_for(slug),
            "seeded_sections": SEEDED_SECTIONS}


def save_case(slug: str, *, base: str | None, raw: str | None = None, parts: dict | None = None,
              index: dict | None = None, signatures: list[str] | None = None) -> str:
    """Write a case and, when given, its index row and signatures. Everything is checked
    before anything is written, and `base` (the etag it was opened at) refuses an overwrite
    of somebody else's newer save."""
    with LOCK:
        p = case_path(slug)
        current = p.read_text(encoding="utf-8")
        if base and base != etag(current):
            raise KbError("someone else saved this case after you opened it; reopen it and "
                          "make your change again")
        if raw is not None:
            text = raw if raw.endswith("\n") else raw + "\n"
            validate_raw(text)
        else:
            text = join(validate_parts(parts or {}))
        if index is not None:
            _check_cell(index.get("says"), "PM usually says")
            _check_cell(index.get("evidence"), "decisive triage evidence")
        if signatures is not None:
            for x in signatures:
                if x.strip():
                    check_regex(x.strip())
        if text != current:
            write_atomic(p, text)
        if index is not None:
            set_index_row(slug, index["says"], index["evidence"])
        if signatures is not None:
            set_signatures(slug, signatures)
        return etag(text)


def create_case(name: str, topics: list[str]) -> str:
    slug = slugify(name)
    if not slug:
        raise KbError("the name needs at least one letter or digit")
    with LOCK:
        p = case_path(slug, must_exist=False)
        if p.exists():
            raise FileExistsError(slug)
        title = name.strip() if name.strip() and name.strip() != slug else \
            slug.replace("-", " ").capitalize()
        parts = validate_parts({"topics": topics, "symptoms": [], "title": title,
                                "sections": [{"heading": h, "body": ""} for h in SEEDED_SECTIONS]})
        write_atomic(p, join(parts))
    return slug


def references(slug: str) -> list[str]:
    """Other KB files that mention the slug, which a rename or delete leaves pointing nowhere."""
    hits = []
    for p in sorted([S.skill_dir / "SKILL.md", *S.skill_dir.glob("kb/*.md"),
                     *S.cases_dir.glob("*.md")]):
        if p.stem == slug and p.parent == S.cases_dir:
            continue
        text = p.read_text(encoding="utf-8")
        if p.name == "SKILL.md":
            lines = text.split("\n")
            a, b = _index_bounds(lines)
            text = "\n".join(lines[:a] + lines[b:])
        if re.search(rf"(?<![a-z0-9-]){re.escape(slug)}(?![a-z0-9-])", text):
            hits.append(str(p.relative_to(S.skill_dir)))
    return hits


def rename_case(slug: str, new_name: str) -> str:
    new = slugify(new_name)
    if not new:
        raise KbError("the new name needs at least one letter or digit")
    with LOCK:
        src = case_path(slug)
        dst = case_path(new, must_exist=False)
        if dst.exists():
            raise FileExistsError(new)
        set_index_row(slug, None, None, rename_to=new)
        set_signatures(slug, None, rename_to=new)
        os.replace(src, dst)
    return new


def delete_case(slug: str) -> None:
    with LOCK:
        p = case_path(slug)
        set_index_row(slug, None, None, remove=True)
        set_signatures(slug, [])
        p.unlink()


# --------------------------------------------------------------------------
# Reference docs
# --------------------------------------------------------------------------

DOCS = {"system-model": "kb/system-model.md", "glossary": "kb/glossary.md",
        "escalation": "kb/escalation.md", "signatures": "scripts/signatures.txt",
        "skill": "SKILL.md"}
READ_ONLY_DOCS = {"skill"}


def doc_path(name: str) -> Path:
    if name not in DOCS:
        raise FileNotFoundError(name)
    return S.skill_dir / DOCS[name]


def get_doc(name: str) -> dict:
    p = doc_path(name)
    text = p.read_text(encoding="utf-8")
    return {"name": name, "path": DOCS[name], "text": text, "etag": etag(text),
            "read_only": name in READ_ONLY_DOCS}


def save_doc(name: str, text: str, base: str | None) -> str:
    if name in READ_ONLY_DOCS:
        raise KbError(f"{DOCS[name]} is read-only here; its index rows are edited with each case")
    with LOCK:
        p = doc_path(name)
        current = p.read_text(encoding="utf-8")
        if base and base != etag(current):
            raise KbError("someone else saved this file after you opened it; reopen it and "
                          "make your change again")
        if not text.endswith("\n"):
            text += "\n"
        if name == "signatures":
            check_signatures_text(text)
        if text != current:
            write_atomic(p, text)
        return etag(text)


# --------------------------------------------------------------------------
# selftest.sh
# --------------------------------------------------------------------------

def selftest() -> dict:
    """The skill's own offline checks: signatures <-> cases <-> index, scripts parse, redaction."""
    try:
        r = subprocess.run([str(S.skill_dir / "scripts" / "selftest.sh")], cwd=S.skill_dir,
                           capture_output=True, text=True, timeout=120)
        out = r.stdout + r.stderr
        return {"ok": r.returncode == 0, "failures": [x[6:] for x in out.splitlines()
                                                       if x.startswith("FAIL  ")],
                "output": out[-20000:]}
    except (OSError, subprocess.TimeoutExpired) as e:
        return {"ok": False, "failures": [f"selftest did not run: {e}"], "output": ""}
