"""Knowledge-base editing endpoints, and the guard that keeps writes inside kb/."""
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


@pytest.fixture
def client(tmp_path, monkeypatch):
    import console.server as srv
    monkeypatch.setattr(srv, "KB", tmp_path)
    (tmp_path / "seed.md").write_text("# Seed\n")
    return TestClient(srv.app)


def test_create_edit_read_delete(client):
    assert client.post("/api/kb", json={"name": "new-entry"}).json()["ok"]
    body = client.get("/api/kb/new-entry").json()["text"]
    assert "## Symptoms" in body, "new docs start from the runbook skeleton"

    client.put("/api/kb/new-entry", json={"text": "# Edited\n"})
    assert client.get("/api/kb/new-entry").json()["text"] == "# Edited\n"

    assert {d["name"] for d in client.get("/api/kb").json()["docs"]} == {"seed", "new-entry"}
    client.delete("/api/kb/new-entry")
    assert [d["name"] for d in client.get("/api/kb").json()["docs"]] == ["seed"]


def test_duplicate_create_is_refused(client):
    assert client.post("/api/kb", json={"name": "seed"}).status_code == 409


@pytest.mark.parametrize("name", [
    "../../etc/passwd", "..%2f..%2fetc%2fpasswd", "Bad_Name", "UPPER",
    "with space", "", "a" * 80, ".hidden", "sub/dir",
])
def test_writes_cannot_escape_the_kb_directory(client, tmp_path, name):
    r = client.put(f"/api/kb/{name}", json={"text": "pwned"})
    assert r.status_code in (400, 404, 405), f"{name!r} was accepted"
    assert not any(p.read_text() == "pwned" for p in tmp_path.rglob("*") if p.is_file())


def test_edited_knowledge_reaches_the_cached_prompt(tmp_path, monkeypatch):
    """The KB is the system prompt; an edit must actually change what the model sees."""
    import llm
    monkeypatch.setattr(llm, "KB_DIR", tmp_path)
    (tmp_path / "a.md").write_text("ORIGINAL GUIDANCE")
    assert "ORIGINAL GUIDANCE" in llm._kb()
    (tmp_path / "a.md").write_text("REPLACED GUIDANCE")
    assert "REPLACED GUIDANCE" in llm._kb()
    assert "ORIGINAL GUIDANCE" not in llm._kb()
