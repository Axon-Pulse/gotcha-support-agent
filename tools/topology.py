"""eCAL pub/sub wiring: who publishes, who subscribes, what is silent.

`ecal_mon_cli -l` reprints every topic on each refresh, so the raw dump is ~22k lines
for ~92 distinct endpoints. Dedupe by topic id (keeping the last snapshot), then
aggregate by topic name. Raw output never reaches the model.
"""
import collections
import re

import transport
from registry import tool

_FIELD = re.compile(r"^(topic_name|ttype name|direction|host name|process id|topic id|data frequency)\s*:\s*(.*)$")


def _parse(text: str) -> list[dict]:
    """Split the dump into one record per blank-line-separated block."""
    rows, cur = [], {}
    for line in text.splitlines():
        m = _FIELD.match(line.strip())
        if m:
            cur[m.group(1)] = m.group(2).strip()
        elif not line.strip() and cur:
            rows.append(cur)
            cur = {}
    if cur:
        rows.append(cur)
    return [r for r in rows if "topic_name" in r and "topic id" in r]


def _dedupe(rows: list[dict]) -> dict[str, dict]:
    """Last snapshot wins, keyed by topic id."""
    return {r["topic id"]: r for r in rows}


@tool({
    "name": "get_ecal_topology",
    "description": (
        "eCAL publish/subscribe map for the running system. Reveals three things nothing "
        "else can see: (1) duplicate_instances - two independent sets of processes "
        "publishing the same topics means two launcher sessions are running at once; "
        "(2) dangling_subscriptions - a topic with subscribers but no publisher, meaning "
        "the node that should publish it is not running; (3) silent_publishers - a "
        "publisher present but sending at 0 Hz."
    ),
    "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
})
def get_ecal_topology() -> dict:
    rows = list(_dedupe(_parse(transport.run("topology"))).values())

    by_topic: dict[str, dict] = {}
    pid_topics: dict[str, set] = collections.defaultdict(set)
    for r in rows:
        t = by_topic.setdefault(r["topic_name"], {
            "type": r.get("ttype name", ""), "pubs": 0, "subs": 0, "max_hz": 0.0})
        t["pubs" if r.get("direction") == "publisher" else "subs"] += 1
        try:
            t["max_hz"] = max(t["max_hz"], float(r.get("data frequency", 0) or 0))
        except ValueError:
            pass
        if r.get("direction") == "publisher":
            pid_topics[r["process id"]].add(r["topic_name"])

    # Two launcher sessions => two disjoint PID groups with identical publish sets.
    sig = collections.defaultdict(list)
    for pid, topics in pid_topics.items():
        sig[frozenset(topics)].append(pid)
    dup = [{"topics": sorted(k), "pids": sorted(v)}
           for k, v in sig.items() if len(v) > 1 and k]

    return {
        "hosts": sorted({r.get("host name", "?") for r in rows}),
        "processes": len(pid_topics),
        "unique_endpoints": len(rows),
        "duplicate_instances": {
            "detected": bool(dup),
            "groups": [{"pids": d["pids"], "topic_count": len(d["topics"])} for d in dup],
        },
        "topics": [
            {"topic": k, "type": v["type"], "pubs": v["pubs"],
             "subs": v["subs"], "hz": round(v["max_hz"], 3)}
            for k, v in sorted(by_topic.items())
        ],
        "dangling_subscriptions": sorted(
            k for k, v in by_topic.items() if v["subs"] and not v["pubs"]),
        "silent_publishers": sorted(
            k for k, v in by_topic.items() if v["pubs"] and v["max_hz"] == 0.0),
    }
