"""Which model answers is an operator choice, and it has to reach the next call.

Two things are protected here. The model is read PER CALL, not frozen at import — a
console edit that only changed a module constant would leave the pill saying one thing
while the process kept sending another. And a chosen id is validated against the
account's own catalogue, because an id that does not exist fails minutes later as an
opaque 404 in the middle of a diagnosis rather than at the moment somebody picked it.
"""
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import config_store  # noqa: E402
import llm  # noqa: E402

CATALOGUE = [
    {"id": "claude-opus-5", "display_name": "Claude Opus 5", "max_input_tokens": 1000000},
    {"id": "claude-sonnet-5", "display_name": "Claude Sonnet 5"},
    {"id": "claude-haiku-4-5", "display_name": "Claude Haiku 4.5"},
]


@pytest.fixture
def client(monkeypatch, tmp_path):
    import console.server as srv
    monkeypatch.setattr(config_store, "PATH", tmp_path / "overrides.json")
    monkeypatch.setattr(config_store, "AUDIT", tmp_path / "audit.jsonl")
    monkeypatch.setattr(llm, "models",
                        lambda refresh=False: {"models": list(CATALOGUE), "source": "api"})
    return TestClient(srv.app)


# ---------- the effective model ----------

def test_the_model_is_read_per_call_not_frozen_at_import(monkeypatch, tmp_path):
    """The whole point: an override must reach the very next request."""
    monkeypatch.setattr(config_store, "PATH", tmp_path / "overrides.json")
    monkeypatch.setattr(config_store, "AUDIT", tmp_path / "audit.jsonl")
    assert llm.model() == llm.DEFAULT_MODEL
    config_store.put("model", "claude-sonnet-5")
    assert llm.model() == "claude-sonnet-5", "the override did not reach the next call"


def test_clearing_the_override_falls_back_to_the_default(monkeypatch, tmp_path):
    monkeypatch.setattr(config_store, "PATH", tmp_path / "overrides.json")
    monkeypatch.setattr(config_store, "AUDIT", tmp_path / "audit.jsonl")
    config_store.put("model", "claude-haiku-4-5")
    config_store.clear("model")
    assert llm.model() == llm.DEFAULT_MODEL


def test_an_empty_override_is_not_sent_as_a_model(monkeypatch, tmp_path):
    """An empty string is a cleared field, not a model id; sending it is a 404."""
    monkeypatch.setattr(config_store, "PATH", tmp_path / "overrides.json")
    monkeypatch.setattr(config_store, "AUDIT", tmp_path / "audit.jsonl")
    config_store.put("model", "")
    assert llm.model() == llm.DEFAULT_MODEL


def test_every_call_site_asks_for_the_model():
    """A call site left on the old constant would silently ignore the setting.

    Counted against the requests themselves rather than a fixed number: a new call site
    is a normal thing to add, and a test that breaks when one appears gets its number
    bumped without anybody checking the new site actually asks.
    """
    src = (ROOT / "llm.py").read_text()
    assert "model=MODEL" not in src, "a request still uses the frozen constant"
    requests = src.count("messages.create(")
    asking = src.count("model=model()")
    assert requests and asking == requests, (
        f"{requests} requests but {asking} ask for the model — one is pinned to "
        f"something the console cannot change")


# ---------- the catalogue ----------

def test_a_missing_key_leaves_a_usable_menu(monkeypatch):
    """No credentials must not mean an empty dropdown."""
    def boom():
        raise RuntimeError("no credentials")
    monkeypatch.setattr(llm, "client", boom)
    monkeypatch.setattr(llm, "_catalogue", None)
    got = llm.models(refresh=True)
    assert got["source"] == "fallback"
    assert [m["id"] for m in got["models"]] == [m["id"] for m in llm.FALLBACK_MODELS]
    assert "no credentials" in got["why"], "did not say why the list is the built-in one"


def test_the_catalogue_comes_from_the_account_when_it_can(monkeypatch):
    class M:
        def __init__(self, i): self.id, self.display_name = i, i.upper()
    monkeypatch.setattr(llm, "_catalogue", None)
    monkeypatch.setattr(llm, "client", lambda: type(
        "C", (), {"models": type("Ms", (), {"list": staticmethod(lambda: [M("m-1")])})()})())
    got = llm.models(refresh=True)
    assert got["source"] == "api" and got["models"][0]["id"] == "m-1"
    monkeypatch.setattr(llm, "_catalogue", None)


def test_an_account_listing_nothing_falls_back(monkeypatch):
    """An empty list is not an answer — it would leave nothing to choose."""
    monkeypatch.setattr(llm, "_catalogue", None)
    monkeypatch.setattr(llm, "client", lambda: type(
        "C", (), {"models": type("Ms", (), {"list": staticmethod(lambda: [])})()})())
    assert llm.models(refresh=True)["source"] == "fallback"
    monkeypatch.setattr(llm, "_catalogue", None)


# ---------- the console surface ----------

def test_the_menu_lists_models_and_marks_the_current_one(client):
    got = client.get("/api/models").json()
    assert [m["id"] for m in got["models"]] == [m["id"] for m in CATALOGUE]
    assert got["current"] == llm.model()
    assert got["default"] == llm.DEFAULT_MODEL


def test_choosing_a_model_takes_effect_and_is_audited(client, tmp_path):
    r = client.put("/api/model", json={"model": "claude-sonnet-5"})
    assert r.status_code == 200 and r.json()["current"] == "claude-sonnet-5"
    assert llm.model() == "claude-sonnet-5", "the choice did not reach llm"
    assert client.get("/api/meta").json()["model"] == "claude-sonnet-5", \
        "the header would keep showing the old model"
    audit = (tmp_path / "audit.jsonl").read_text()
    assert "claude-sonnet-5" in audit, "a cost-changing edit left no audit trail"


def test_an_unknown_model_is_refused_at_the_moment_it_is_chosen(client):
    """Not minutes later, as a 404 in the middle of a diagnosis."""
    r = client.put("/api/model", json={"model": "claude-opus-5-20260401"})
    assert r.status_code == 400
    assert "not in this account's model list" in r.json()["detail"]
    assert llm.model() != "claude-opus-5-20260401"


def test_a_refusal_on_a_fallback_list_says_the_list_may_be_incomplete(client, monkeypatch):
    """Blocking an operator whose account has a model we could not list needs explaining."""
    monkeypatch.setattr(llm, "models", lambda refresh=False: {
        "models": list(llm.FALLBACK_MODELS), "source": "fallback", "why": "no credentials"})
    r = client.put("/api/model", json={"model": "claude-fable-5-1"})
    assert r.status_code == 400
    detail = r.json()["detail"]
    assert "could not be fetched" in detail and "no credentials" in detail


def test_the_model_can_be_reset(client):
    client.put("/api/model", json={"model": "claude-haiku-4-5"})
    assert client.delete("/api/model").json()["current"] == llm.DEFAULT_MODEL
    assert llm.model() == llm.DEFAULT_MODEL


def test_a_blank_model_is_refused(client):
    assert client.put("/api/model", json={"model": "   "}).status_code == 400
