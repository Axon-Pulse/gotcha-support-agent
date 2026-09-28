"""Graph state.

`messages` is the model's working memory and is lossy. `findings` is the durable typed
record: the report cites finding ids, so every claim can be checked against a structure
rather than against prose the model wrote about itself.
"""
import operator
from dataclasses import dataclass, field
from typing import Annotated, Any, TypedDict


@dataclass
class Finding:
    id: str
    agent: str
    tool: str
    ok: bool
    data: dict[str, Any] = field(default_factory=dict)


@dataclass
class Scenario:
    """A proposed new knowledge-base entry. Data only until a human approves it."""
    id: str
    title: str
    symptoms: list[str]
    root_cause: str
    checks: list[str]
    fix: str


def merge_context(a: dict, b: dict) -> dict:
    """Later extractions win, but never overwrite a real value with an empty one."""
    out = dict(a or {})
    for k, v in (b or {}).items():
        if v or k not in out:
            out[k] = v
    return out


class S(TypedDict, total=False):
    question: str
    session_id: str
    # The one system this session is about, or None where there is only one machine.
    # Every agent step runs with it active — see inventory.using().
    system: str | None
    findings: Annotated[list[Finding], operator.add]
    visited: Annotated[list[str], operator.add]
    transcript: Annotated[list[dict], operator.add]
    # Facts extracted from findings, used to gate agents that depend on them.
    # An agent whose needs_context is unmet can never be picked — see graph._eligible.
    context: Annotated[dict, merge_context]
    blocked: Annotated[list[dict], operator.add]
    next: str
    report: dict
    customer_message: dict | None
    scenario: dict | None
    saved: bool
