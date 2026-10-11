"""The Slack gateway: who may ask, what they get back, and what stays out of Slack."""
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import slack_app as S  # noqa: E402

REPORT = {"root_cause": "The acoustic backend was never reachable", "confidence": "high",
          "escalate": False, "evidence": ["asu_connected=false", "success_count 0"],
          "unknowns": ["whether the container was ever started"],
          "suggested_actions": ["docker ps -a --filter name=dumbo"]}
DRAFT = {"text": "Your acoustic sensor could not reach a service it relies on.",
         "safe_to_send": True, "leaks": [], "review_reason": ""}
OUT = {"report": REPORT, "customer_message": DRAFT,
       "blocked": [{"agent": "network", "note": "network needs probe_targets, which "
                                                "triage did not produce."}],
       "transcript": [{"agent": "triage"}, {"agent": "knowledge"}],
       "findings": [{"tool": "get_system_health", "ok": True},
                    {"tool": "get_process_table", "ok": False}]}


@pytest.fixture
def posts(monkeypatch):
    sent = []
    monkeypatch.setattr(S, "post", lambda ch, text, thread=None: sent.append(
        {"channel": ch, "text": text, "thread": thread}))
    return sent


@pytest.fixture(autouse=True)
def clean(monkeypatch, tmp_path):
    monkeypatch.setattr(S, "TRACES", tmp_path / "traces")
    monkeypatch.setattr(S, "ALLOWED_CHANNELS", {"C_OPS"})
    monkeypatch.setattr(S, "ALLOWED_USERS", set())
    monkeypatch.setattr(S, "APPROVERS", set())
    monkeypatch.setattr(S, "ADMIN_CHANNEL", "C_ADMIN")
    monkeypatch.setattr(S, "ALLOW_DMS", False)
    S._seen.clear(); S._sessions.clear(); S._last_request.clear()


# ---------- 1. the audience shift ----------

def test_tier_one_gets_the_technical_report(posts):
    S._deliver("s1", OUT, "C_OPS", "111.1")
    text = posts[0]["text"]
    assert "The acoustic backend was never reachable" in text
    assert "asu_connected=false" in text, "evidence must quote real tool output"
    assert "get_system_health" in text, "which tools ran, and which failed"
    assert "network needs probe_targets" in text, "what could not be checked"


def test_the_customer_draft_is_not_posted_to_the_engineer(posts):
    """customer_communicator strips node names, IPs, PIDs and tool names — which is
    exactly what an engineer needs. It must not be the default reply."""
    S._deliver("s1", OUT, "C_OPS", "111.1")
    body = " ".join(p["text"] for p in posts)
    assert DRAFT["text"] not in body
    assert "/draft s1" in body, "but its existence is advertised"


def test_the_draft_is_available_on_request(tmp_path):
    S._remember("s2", customer_message=DRAFT)
    assert S.draft_for("s2")["text"] == DRAFT["text"]


def test_the_draft_survives_a_restart_via_the_trace(tmp_path):
    S._write_trace("s3", OUT, {"user": "U1", "channel": "C_OPS"})
    S._sessions.clear()
    assert S.draft_for("s3")["text"] == DRAFT["text"]
    assert S.draft_for("nope") is None


def test_mock_mode_is_called_out_in_the_report(monkeypatch):
    monkeypatch.setattr(S.transport, "MODE", "mock")
    assert "mock mode" in S.format_report("s1", OUT)
    monkeypatch.setattr(S.transport, "MODE", "live")
    assert "mock mode" not in S.format_report("s1", OUT)


def test_an_escalating_report_is_marked(posts):
    S._deliver("s1", {**OUT, "report": {**REPORT, "escalate": True,
                                        "escalate_reason": "ambiguous route"}},
               "C_OPS", "111.1")
    assert "escalate" in posts[0]["text"] and "ambiguous route" in posts[0]["text"]


# ---------- 2. admission ----------

def test_redelivery_does_not_run_the_diagnosis_twice(monkeypatch):
    started = []
    monkeypatch.setattr(S, "start_session", lambda *a: started.append(a) or "sid")
    assert S.accept("E1", "U1", "C_OPS", "1.1", "no tracks")["status"] == "accepted"
    assert S.accept("E1", "U1", "C_OPS", "1.1", "no tracks")["status"] == "duplicate"
    assert len(started) == 1


def test_an_unconfigured_allowlist_refuses_everyone(monkeypatch):
    """Fail closed: workspace membership is not authorisation."""
    monkeypatch.setattr(S, "ALLOWED_CHANNELS", set())
    monkeypatch.setattr(S, "ALLOWED_USERS", set())
    ok, why = S.authorise("U1", "C_OPS")
    assert ok is False and "no allowlist configured" in why


@pytest.mark.parametrize("user,channel,ok", [
    ("U1", "C_OPS", True),
    ("U1", "C_RANDOM", False),
    ("U1", "D_DM", False),
])
def test_channel_allowlist(user, channel, ok):
    assert S.authorise(user, channel)[0] is ok


def test_a_user_allowlist_narrows_further(monkeypatch):
    monkeypatch.setattr(S, "ALLOWED_USERS", {"U_OK"})
    assert S.authorise("U_OK", "C_OPS")[0] is True
    assert S.authorise("U_OTHER", "C_OPS")[0] is False


def test_dms_are_off_unless_opted_in_and_named(monkeypatch):
    """Anyone in the workspace can DM a bot."""
    monkeypatch.setattr(S, "ALLOW_DMS", True)
    monkeypatch.setattr(S, "ALLOWED_USERS", {"U_OK"})
    assert S.authorise("U_OK", "D_DM")[0] is True
    assert S.authorise("U_OTHER", "D_DM")[0] is False


def test_a_denied_request_never_reaches_the_graph(monkeypatch):
    monkeypatch.setattr(S, "start_session",
                        lambda *a: pytest.fail("the graph ran for a denied request"))
    assert S.accept("E9", "U1", "C_RANDOM", "1.1", "hello")["status"] == "denied"


def test_one_person_cannot_spam_the_model(monkeypatch):
    monkeypatch.setattr(S, "start_session", lambda *a: "sid")
    assert S.accept("E1", "U1", "C_OPS", "1.1", "q")["status"] == "accepted"
    assert S.accept("E2", "U1", "C_OPS", "1.1", "q")["status"] == "rate_limited"
    assert S.accept("E3", "U2", "C_OPS", "1.1", "q")["status"] == "accepted", "per user"


# ---------- 3. one message, one session ----------

def test_every_message_gets_a_fresh_session(monkeypatch):
    """Reusing a checkpoint re-summarises the OLD evidence against the NEW question:
    findings and visited are append-only, so no agent is eligible the second time."""
    ids = []
    monkeypatch.setattr(S.threading, "Thread",
                        lambda *a, **k: type("T", (), {"start": lambda self: None})())
    for e in ("E1", "E2"):
        S._last_request.clear()
        ids.append(S.accept(e, "U1", "C_OPS", "same.thread", "q")["session_id"])
    assert ids[0] != ids[1], "same Slack thread, different graph threads"


def test_the_gateway_never_resumes_a_checkpoint_for_a_new_question():
    src = (ROOT / "slack_app.py").read_text()
    resume_calls = src.count("Command(resume=")
    assert resume_calls == 1, "only the approval path may resume"
    assert "def resume_approval" in src[:src.index("Command(resume=")]


# ---------- 4. tracing ----------

def test_a_slack_session_leaves_an_audit_trail(tmp_path):
    S._write_trace("s9", OUT, {"user": "U1", "channel": "C_OPS", "question": "no tracks"})
    row = json.loads((tmp_path / "traces" / "s9.jsonl").read_text().strip())
    assert row["origin"]["via"] == "slack"
    assert row["origin"]["user"] == "U1" and row["origin"]["channel"] == "C_OPS"
    assert row["report"]["root_cause"] == REPORT["root_cause"]
    assert row["blocked"] and "mode" in row["origin"]


# ---------- 5. approval stays an admin decision ----------

def test_approval_is_console_only_by_default():
    msg = S.resume_approval("s1", "U_ANY", "C_ADMIN", True)
    assert "console" in msg


def test_only_named_approvers_may_approve(monkeypatch):
    monkeypatch.setattr(S, "APPROVERS", {"U_ADMIN"})
    assert "not an approver" in S.resume_approval("s1", "U_T1", "C_ADMIN", True)


def test_approval_is_refused_outside_the_admin_channel(monkeypatch):
    monkeypatch.setattr(S, "APPROVERS", {"U_ADMIN"})
    assert "admin channel" in S.resume_approval("s1", "U_ADMIN", "C_OPS", True)


def test_the_approval_request_goes_to_the_admin_channel_not_the_engineer(posts):
    out = {**OUT, "__interrupt__": [type("I", (), {"value": {"preview_md": "# A case"}})()]}
    S._deliver("s1", out, "C_OPS", "111.1")
    where = {p["channel"] for p in posts if "runbook case proposed" in p["text"]}
    assert where == {"C_ADMIN"}


# ---------- 6. the seam the split creates ----------

def test_the_graph_is_rebuilt_when_the_console_edits_the_config(monkeypatch):
    """Otherwise an admin reordering agents sees no effect here until a restart."""
    import config_store
    builds = []
    monkeypatch.setattr(S, "build", lambda: builds.append(1) or object())
    monkeypatch.setattr(S, "_graph", None)
    monkeypatch.setattr(S, "_graph_sig", None)
    S.graph(); S.graph()
    assert len(builds) == 1, "no rebuild while the config is unchanged"
    config_store.put("supervisor_picks", False)
    S.graph()
    assert len(builds) == 2, "an edit in the console reaches the bot"


# ---------- 7. the adapter stays an adapter ----------

def test_socket_mode_dropped_signature_verification_but_kept_dedup():
    src = (ROOT / "slack_app.py").read_text()
    assert "def verify" not in src, "Socket Mode authenticates the connection itself"
    assert "SIGNING_SECRET" not in src
    assert "def already_handled" in src, "redelivery still happens"


def test_the_module_imports_without_slack_bolt():
    """Bolt is confined to build_app(), so the gateway is testable and the graph is not
    coupled to a Slack dependency."""
    import importlib
    assert "slack_bolt" not in sys.modules
    assert importlib.import_module("slack_app") is S
