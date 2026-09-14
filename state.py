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


class S(TypedDict, total=False):
    question: str
    session_id: str
    findings: Annotated[list[Finding], operator.add]
    visited: Annotated[list[str], operator.add]
    transcript: Annotated[list[dict], operator.add]
    next: str
    report: dict
    scenario: dict | None
    saved: bool
