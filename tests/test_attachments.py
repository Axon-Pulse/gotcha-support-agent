"""Attachments: a screenshot reaches the agents as text, described once on upload."""
import base64
import json

import pytest
from fastapi.testclient import TestClient

import attachments as att
import bridge
import conversation as convo

PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")
USAGE = {"input": 1500, "output": 200, "cache_read": 0}


def b64(b: bytes) -> str:
    return base64.b64encode(b).decode()


def fake_describe(data, media_type, name):
    fake_describe.calls.append((media_type, name, len(data)))
    return "Map view. Error banner: 'ASU backend unreachable'.", dict(USAGE)


@pytest.fixture(autouse=True)
def _dir(tmp_path, monkeypatch):
    monkeypatch.setattr(att, "DIR", tmp_path / "attachments")
    fake_describe.calls = []


# ---------- the store ----------

def test_an_image_is_described_once_and_kept():
    rec = att.save("shot.png", "image/png", b64(PNG), describe=fake_describe)
    assert rec["kind"] == "image" and "ASU backend unreachable" in rec["text"]
    assert rec["usage"] == USAGE and fake_describe.calls == [("image/png", "shot.png", len(PNG))]
    assert att.file_path(rec["id"]).read_bytes() == PNG
    assert att.load(rec["id"])["text"] == rec["text"]


def test_a_pdf_goes_to_the_describer_as_a_pdf():
    att.save("report.pdf", "application/pdf", b64(b"%PDF-1.4 x"), describe=fake_describe)
    assert fake_describe.calls[0][0] == "application/pdf"


def test_a_text_file_is_its_own_description_and_costs_nothing():
    rec = att.save("launcher.log", "", b64(b"PROC_EXITED_ERROR magos\n"), describe=fake_describe)
    assert rec["kind"] == "text" and rec["text"] == "PROC_EXITED_ERROR magos"
    assert fake_describe.calls == [] and rec["usage"]["input"] == 0


def test_a_long_text_file_is_cut_and_says_so():
    rec = att.save("big.log", "text/plain", b64(b"x" * (att.MAX_TEXT_CHARS + 50)))
    assert rec["truncated"] and "cut: the first 20,000 of 20,050" in rec["text"]


@pytest.mark.parametrize("name,mt,data,msg", [
    ("a.exe", "application/octet-stream", b64(b"MZ"), "not supported"),
    ("a.png", "image/png", "!!not base64!!", "not valid base64"),
    ("a.png", "image/png", "", "empty"),
])
def test_bad_uploads_are_refused_with_a_reason(name, mt, data, msg):
    with pytest.raises(att.AttachmentError, match=msg):
        att.save(name, mt, data, describe=fake_describe)


def test_too_big_is_refused(monkeypatch):
    monkeypatch.setattr(att, "MAX_BYTES", 10)
    with pytest.raises(att.AttachmentError, match="over the"):
        att.save("a.png", "image/png", b64(PNG), describe=fake_describe)


def test_an_empty_description_is_an_error_not_a_blank_attachment():
    with pytest.raises(att.AttachmentError, match="no description"):
        att.save("a.png", "image/png", b64(PNG), describe=lambda *a: ("", dict(USAGE)))


def test_ids_cannot_walk_out_of_the_directory():
    with pytest.raises(att.AttachmentError):
        att.load("../../etc")


def test_compose_puts_each_attachment_after_what_was_typed():
    rec = att.save("shot.png", "image/png", b64(PNG), describe=fake_describe)
    text = att.compose("no tracks on the map", [rec])
    assert text.startswith("no tracks on the map\n\n[Attached screenshot / image: shot.png")
    assert "did not see the picture itself" in text and "ASU backend unreachable" in text
    assert att.compose("", [rec]).startswith("[Attached")


# ---------- follow-ups still see them ----------

def test_a_later_turn_is_answered_knowing_about_the_attachment():
    rec = att.public([att.save("shot.png", "image/png", b64(PNG), describe=fake_describe)])
    d = convo.digest([{"kind": "run", "question": "no tracks", "attachments": rec,
                       "report": {}, "findings": []}])
    assert "ASU backend unreachable" in d


# ---------- the console ----------

@pytest.fixture
def client(tmp_path, monkeypatch):
    import console.server as srv
    monkeypatch.setattr(srv, "TRACES", tmp_path / "traces")
    monkeypatch.setattr(srv, "DB", str(tmp_path / "graph.db"))
    monkeypatch.setattr(srv, "_describe", fake_describe)
    monkeypatch.setattr(srv, "_credentials", lambda: {"ready": True})
    return srv, TestClient(srv.app)


def test_upload_describes_and_serves_the_original(client):
    srv, c = client
    r = c.post("/api/attachments", json={"name": "shot.png", "media_type": "image/png",
                                         "data": b64(PNG)})
    assert r.status_code == 200 and "ASU backend" in r.json()["text"]
    f = c.get(f"/api/attachments/{r.json()['id']}/file")
    assert f.status_code == 200 and f.content == PNG
    assert f.headers["content-type"] == "image/png"


def test_an_unsupported_upload_is_a_400_with_the_reason(client):
    _, c = client
    r = c.post("/api/attachments", json={"name": "a.exe", "data": b64(b"MZ")})
    assert r.status_code == 400 and "not supported" in r.json()["detail"]


def test_a_run_reads_the_description_and_the_trace_keeps_them_apart(client, tmp_path,
                                                                    monkeypatch):
    srv, c = client
    rec = c.post("/api/attachments", json={"name": "shot.png", "media_type": "image/png",
                                           "data": b64(PNG)}).json()
    # Drive the real _run, but with a graph that fails at once: the start line is what
    # this test is about, and it is written before the graph is touched.
    monkeypatch.setattr(srv, "graph", lambda: (_ for _ in ()).throw(RuntimeError("stop")))
    sid = c.post("/api/run", json={"question": "no tracks",
                                   "attachments": [rec["id"]]}).json()["session_id"]
    import time
    for _ in range(50):
        p = tmp_path / "traces" / f"{sid}.jsonl"
        if p.exists() and len(p.read_text().splitlines()) >= 2:
            break
        time.sleep(0.05)
    lines = [json.loads(l) for l in p.read_text().splitlines()]
    start = lines[0]
    assert start["question"] == "no tracks"
    assert start["attachments"][0]["name"] == "shot.png"
    assert start["usage"] == USAGE, "the description's tokens never joined the session"
    row = c.get("/api/traces").json()["traces"][0]
    assert row["tokens"] >= 1700
    detail = c.get(f"/api/traces/{sid}").json()
    assert detail["summary"]["attachments"][0]["id"] == rec["id"]
    assert detail["summary"]["problem"] == "no tracks"


def test_an_attachment_alone_is_enough_to_ask(client):
    srv, c = client
    rec = c.post("/api/attachments", json={"name": "shot.png", "media_type": "image/png",
                                           "data": b64(PNG)}).json()
    cid = c.post("/api/conversation").json()["conversation_id"]
    got = []
    srv_converse = srv._converse
    srv._converse = lambda cid, msg, recs, system=None: got.append((msg, recs))
    try:
        assert c.post(f"/api/conversation/{cid}/message",
                      json={"message": "", "attachments": [rec["id"]]}).status_code == 200
    finally:
        srv._converse = srv_converse
    assert got and got[0][0] == "" and got[0][1][0]["id"] == rec["id"]
    assert c.post(f"/api/conversation/{cid}/message", json={"message": ""}).status_code in (400, 409)


def test_an_unknown_attachment_id_is_refused(client):
    _, c = client
    r = c.post("/api/run", json={"question": "q", "attachments": ["0123456789ab"]})
    assert r.status_code == 400 and "does not exist" in r.json()["detail"]


# ---------- bridge tickets ----------

def test_a_bridge_ticket_reads_the_description_and_traces_what_was_typed(tmp_path,
                                                                         monkeypatch):
    monkeypatch.setattr(bridge, "REQUESTS", tmp_path / "req")
    monkeypatch.setattr(bridge, "REPORTS", tmp_path / "rep")
    monkeypatch.setattr(bridge, "TRACES", tmp_path / "traces")
    recs = att.public([att.save("shot.png", "image/png", b64(PNG), describe=fake_describe)])
    sid = bridge.submit(att.compose("no tracks", recs), typed="no tracks", attachments=recs)
    ticket = bridge.status(sid)
    assert "ASU backend unreachable" in ticket["question"], "the chat side never sees it"
    line = json.loads((tmp_path / "traces" / f"{sid}.jsonl").read_text().splitlines()[0])
    assert line["origin"]["question"] == "no tracks" and line["attachments"] == recs
