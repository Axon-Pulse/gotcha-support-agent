"""What the bot was asked and what it did: Claude's transcripts, plus the bridge's turn log.

The conversation is Claude Code's own transcript, <session_id>.jsonl: each question, every
script the model ran and its output, each file it read, each command the guard refused, and
the answer. A Slack thread is one session (slack-bridge/state/threads.json maps one to the
other).

slack-bridge/state/turns.jsonl adds the Slack side the transcript lacks: who asked, and how
the turn ended. A turn killed at the timeout leaves no answer in its transcript, and a turn
still running has a start line and no end, which Deploy waits for. A logged turn is matched
to its transcript turn by time. Threads from before the log existed come from the
transcripts alone, without the Slack user.
"""
import json
import re
import time
from collections import defaultdict
from pathlib import Path

from console.settings import S

_CASE_READ = re.compile(r"/kb/cases/([a-z0-9][a-z0-9-]*)\.md$")
_cache: dict[Path, tuple[float, int, list]] = {}


def _read_jsonl(path: Path) -> list[dict]:
    out = []
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                try:
                    d = json.loads(line)
                except ValueError:
                    continue          # a line half-written at this moment, or damaged
                if isinstance(d, dict):
                    out.append(d)
    except FileNotFoundError:
        pass
    return out


def timeout_s() -> int:
    """The bridge's BRIDGE_TIMEOUT_S, from its .env, so in-flight is judged as the bot does."""
    from console.ops import bot_env
    try:
        return int(bot_env().get("BRIDGE_TIMEOUT_S") or 180)
    except ValueError:
        return 180


# --------------------------------------------------------------------------
# Turn log
# --------------------------------------------------------------------------

def logged_turns() -> list[dict]:
    """Each start paired with its end. Per thread, turns run one at a time and in order
    (the bridge holds a per-thread lock), so the n-th end belongs to the n-th start."""
    waiting: dict[str, list[dict]] = defaultdict(list)
    turns = []
    now, stale = time.time(), timeout_s() + 30
    for d in _read_jsonl(S.turns_file):
        key = d.get("key")
        if not key:
            continue
        if d.get("event") == "start":
            t = {"key": key, "channel": d.get("channel") or key.split(":")[0],
                 "thread_ts": d.get("thread_ts") or key.split(":", 1)[-1],
                 "user": d.get("user") or "", "ts": d.get("ts"), "ended": None,
                 "model": d.get("model"), "effort": d.get("effort"), "outcome": "running",
                 "session_id": None, "seconds": None, "turns": None, "cost_usd": None,
                 "error": None, "resumed": None}
            waiting[key].append(t)
            turns.append(t)
        elif d.get("event") == "end" and waiting[key]:
            t = waiting[key].pop(0)
            t.update({k: d.get(k) for k in ("session_id", "seconds", "turns", "cost_usd",
                                            "error", "resumed")})
            t["outcome"] = d.get("outcome") or "ok"
            t["ended"] = d.get("ts")
    for t in turns:
        if t["outcome"] == "running" and (t["ts"] or 0) < now - stale:
            t["outcome"] = "lost"       # the bot stopped (restart, crash) before answering
    return turns


def in_flight() -> list[dict]:
    return [t for t in logged_turns() if t["outcome"] == "running"]


# --------------------------------------------------------------------------
# Transcripts
# --------------------------------------------------------------------------

def transcript_path(session_id: str) -> Path | None:
    if not session_id or not re.fullmatch(r"[0-9a-fA-F-]{8,64}", session_id):
        return None
    for d in S.transcript_dirs:
        p = d / f"{session_id}.jsonl"
        if p.exists():
            return p
    return None


def _text_of(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(x.get("text", "") if isinstance(x, dict) else str(x) for x in content)
    return ""


def _tool_summary(name: str, inp: dict) -> str:
    if name == "Bash":
        return inp.get("command", "")
    if name == "Read":
        return inp.get("file_path", "")
    if name == "Skill":
        return f"{inp.get('skill', '')} {inp.get('args', '')}".strip()
    if name in ("Grep", "Glob"):
        return f"{inp.get('pattern', '')} {inp.get('path', '')}".strip()
    return json.dumps(inp, ensure_ascii=False)[:400]


def parse_transcript(path: Path) -> list[dict]:
    """[{question, ts, steps, answer, cost_usd, cases_read}], one per question the session got.

    A turn starts at a user message that is a plain prompt (not a tool result, not the skill
    text Claude Code injects as a meta message). cost-state lines carry the session's running
    total, so a turn's cost is the difference."""
    st = path.stat()
    hit = _cache.get(path)
    if hit and hit[:2] == (st.st_mtime, st.st_size):
        return hit[2]
    turns, cur, by_id, last_cost = [], None, {}, 0.0
    for d in _read_jsonl(path):
        typ = d.get("type")
        if typ == "cost-state" and cur is not None:
            total = d.get("totalCostUSD") or 0.0
            cur["cost_usd"] = round(total - last_cost, 4)
            last_cost = total
            continue
        msg = d.get("message") or {}
        content = msg.get("content")
        if typ == "user" and isinstance(content, str) and not d.get("isMeta"):
            cur = {"question": content, "ts": _iso_ts(d.get("timestamp")), "end_ts": None,
                   "steps": [], "answer": "", "cost_usd": None, "cases_read": []}
            turns.append(cur)
            continue
        if cur is not None and d.get("timestamp") and typ in ("user", "assistant"):
            cur["end_ts"] = _iso_ts(d["timestamp"])
        if cur is None or not isinstance(content, list):
            continue
        for x in content:
            if not isinstance(x, dict):
                continue
            kind = x.get("type")
            if typ == "assistant" and kind == "text" and x.get("text", "").strip():
                cur["steps"].append({"kind": "text", "text": x["text"]})
                cur["answer"] = x["text"]
            elif typ == "assistant" and kind == "tool_use":
                inp = x.get("input") or {}
                step = {"kind": "tool", "id": x.get("id"), "name": x.get("name"),
                        "input": _tool_summary(x.get("name", ""), inp), "result": None,
                        "error": False, "denied": False, "truncated": False}
                cur["steps"].append(step)
                by_id[x.get("id")] = step
                m = _CASE_READ.search(inp.get("file_path", "")) if x.get("name") == "Read" else None
                if m and m.group(1) not in cur["cases_read"]:
                    cur["cases_read"].append(m.group(1))
            elif typ == "user" and kind == "tool_result":
                step = by_id.get(x.get("tool_use_id"))
                if step is not None:
                    # Whole, as the model saw it: Claude Code's own "[... truncated]" marker
                    # stays in the text when it cut something.
                    step["result"] = _text_of(x.get("content"))
                    step["truncated"] = "characters truncated]" in step["result"]
                    step["error"] = bool(x.get("is_error"))
                    step["denied"] = bool(d.get("toolDenialKind")) or "Blocked by" in (step["result"] or "")
    _cache[path] = (st.st_mtime, st.st_size, turns)
    return turns


# --------------------------------------------------------------------------
# Threads
# --------------------------------------------------------------------------

def _thread_map() -> dict[str, str]:
    try:
        d = json.loads(S.threads_file.read_text())
        return d if isinstance(d, dict) else {}
    except (FileNotFoundError, ValueError):
        return {}


def _iso_ts(s: str | None) -> float | None:
    if not s:
        return None
    try:
        from datetime import datetime
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _transcript_turns(sids: list[str]) -> list[dict]:
    out = []
    for sid in sids:
        p = transcript_path(sid)
        if p:
            out += [{**x, "session_id": sid} for x in parse_transcript(p)]
    return out


def _turn(key: str, log: dict | None, tr: dict | None) -> dict:
    """One question and its answer: the Slack side from the log, the rest from the transcript."""
    channel, _, thread_ts = key.partition(":")
    t = {"key": key, "channel": channel, "thread_ts": thread_ts, "user": "", "ts": None,
         "outcome": "unknown", "session_id": None, "seconds": None, "turns": None,
         "cost_usd": None, "error": None, "resumed": None, "question": None, "answer": None,
         "steps": None, "cases_read": [], "source": "log" if log else "transcript"}
    if tr:
        t.update(question=tr["question"], answer=tr["answer"] or None, steps=tr["steps"],
                 cases_read=tr["cases_read"], ts=tr["ts"], session_id=tr["session_id"],
                 cost_usd=tr["cost_usd"], outcome="ok" if tr["answer"] else "unknown")
        if tr["ts"] and tr["end_ts"]:
            t["seconds"] = round(tr["end_ts"] - tr["ts"])
    if log:
        t.update({k: v for k, v in log.items() if k in (
            "user", "ts", "outcome", "error", "resumed", "turns", "model", "effort")})
        for k in ("session_id", "seconds", "cost_usd"):
            if log.get(k) is not None:
                t[k] = log[k]
        if t["outcome"] != "ok":
            t["answer"] = None      # a killed turn's half-written text is not what was posted
    return t


def threads() -> dict[str, list[dict]]:
    """key -> turns, oldest first, each with its question, answer and steps."""
    logged: dict[str, list[dict]] = defaultdict(list)
    for t in logged_turns():
        logged[t["key"]].append(t)
    tmap = _thread_map()
    out = {}
    for key in set(logged) | set(tmap):
        sids = []
        for sid in [t["session_id"] for t in logged.get(key, [])] + [tmap.get(key)]:
            if sid and sid not in sids:
                sids.append(sid)
        pool = _transcript_turns(sids)
        used: set[int] = set()
        turns = []
        for log in logged.get(key, []):
            # The transcript turn that began while this one ran: its question is written the
            # moment claude starts, a little after the log's start line.
            lo, hi = (log["ts"] or 0) - 5, (log.get("ended") or time.time()) + 5
            best = None
            for i, x in enumerate(pool):
                if i not in used and x["ts"] and lo <= x["ts"] <= hi and \
                        (not log["session_id"] or x["session_id"] == log["session_id"]):
                    best = i
                    break
            if best is not None:
                used.add(best)
            turns.append(_turn(key, log, pool[best] if best is not None else None))
        # Transcript turns no log line accounts for: the thread's turns from before the log.
        turns += [_turn(key, None, x) for i, x in enumerate(pool) if i not in used]
        turns.sort(key=lambda t: t["ts"] or 0)
        if turns:
            out[key] = turns
    return out


def user_ids() -> set[str]:
    """Everyone who has asked the bot, as Slack IDs."""
    return {t["user"] for turns in threads().values() for t in turns if t["user"]}


def summaries(q: str = "", outcome: str = "", channel: str = "",
              names: dict[str, str] | None = None) -> list[dict]:
    """The threads, newest first. `q` also finds a thread by who asked, by ID or by name."""
    rows = []
    ql = q.lower().strip()
    names = names or {}
    for key, turns in threads().items():
        if channel and not key.startswith(channel + ":"):
            continue
        if outcome and not any(t["outcome"] == outcome for t in turns):
            continue
        if ql and not any(ql in (t["question"] or "").lower() or ql in (t["answer"] or "").lower()
                          or ql in (t["user"] or "").lower()
                          or ql in names.get(t["user"], "").lower() for t in turns):
            continue
        costs = [t["cost_usd"] for t in turns if isinstance(t["cost_usd"], (int, float))]
        rows.append({"key": key, "channel": turns[0]["channel"], "thread_ts": turns[0]["thread_ts"],
                     "started": turns[0]["ts"], "last": turns[-1]["ts"],
                     "users": sorted({t["user"] for t in turns if t["user"]}),
                     "question": next((t["question"] for t in turns if t["question"]), ""),
                     "count": len(turns), "outcomes": [t["outcome"] for t in turns],
                     "cost_usd": round(sum(costs), 4) if costs else None,
                     "running": any(t["outcome"] == "running" for t in turns),
                     "logged": any(t["source"] == "log" for t in turns)})
    rows.sort(key=lambda r: r["last"] or 0, reverse=True)
    return rows


def thread(key: str) -> dict:
    turns = threads().get(key)
    if not turns:
        raise FileNotFoundError(key)
    sids = list(dict.fromkeys(t["session_id"] for t in turns if t["session_id"]))
    return {"key": key, "channel": turns[0]["channel"], "thread_ts": turns[0]["thread_ts"],
            "sessions": sids, "transcript_found": any(t["steps"] is not None for t in turns),
            "turns": turns}


# --------------------------------------------------------------------------
# Stats
# --------------------------------------------------------------------------

def _pct(xs: list[float], p: float) -> float | None:
    if not xs:
        return None
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(round(p / 100 * (len(xs) - 1))))]


def stats(days: int = 30) -> dict:
    since = time.time() - days * 86400
    all_turns = [t for ts in threads().values() for t in ts if (t["ts"] or 0) >= since]
    per_day: dict[str, dict] = {}
    for t in all_turns:
        day = time.strftime("%Y-%m-%d", time.localtime(t["ts"]))
        d = per_day.setdefault(day, {"day": day, "questions": 0, "cost_usd": 0.0,
                                     "ok": 0, "timeout": 0, "error": 0, "other": 0})
        d["questions"] += 1
        d["cost_usd"] = round(d["cost_usd"] + (t["cost_usd"] or 0), 4)
        d[t["outcome"] if t["outcome"] in ("ok", "timeout", "error") else "other"] += 1
    secs = [t["seconds"] for t in all_turns if isinstance(t.get("seconds"), (int, float))]
    cases: dict[str, int] = defaultdict(int)
    for t in all_turns:
        for c in t["cases_read"]:
            cases[c] += 1
    n = len(all_turns)
    outcomes = defaultdict(int)
    for t in all_turns:
        outcomes[t["outcome"]] += 1
    costs = [t["cost_usd"] for t in all_turns if isinstance(t["cost_usd"], (int, float))]
    # Every day in the window, so a quiet day shows as zero rather than vanishing.
    days_list = []
    for i in range(days - 1, -1, -1):
        day = time.strftime("%Y-%m-%d", time.localtime(time.time() - i * 86400))
        days_list.append(per_day.get(day) or {"day": day, "questions": 0, "cost_usd": 0.0,
                                              "ok": 0, "timeout": 0, "error": 0, "other": 0})
    return {"days": days, "questions": n, "threads": len({t["key"] for t in all_turns}),
            "users": len({t["user"] for t in all_turns if t["user"]}),
            "cost_usd": round(sum(costs), 2), "cost_per_question": round(sum(costs) / len(costs), 3)
            if costs else None, "p50_s": _pct(secs, 50), "p90_s": _pct(secs, 90),
            "outcomes": dict(outcomes), "per_day": days_list,
            "cases_read": sorted(({"slug": k, "count": v} for k, v in cases.items()),
                                 key=lambda r: (-r["count"], r["slug"]))[:15]}
