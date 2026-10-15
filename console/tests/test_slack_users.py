"""Who asked, by name: Slack IDs from the turn log looked up with the bot's token, cached."""
import json
import time

import pytest

from console import slack_users
from console.settings import S
from console.tests.conftest import SID, write_transcript, write_turns

GAL, YON = "U0AFHFYV6R2", "U0BBBBBBBB1"
TOKEN = "xoxb-TEST-TOKEN"
PEOPLE = {GAL: {"real_name": "Gal Badichi", "profile": {"real_name": "Gal Badichi", "display_name": "gal"}},
          YON: {"name": "yonatan", "profile": {"display_name": "Yonatan Shapira"}}}


@pytest.fixture
def slack(env, monkeypatch):
    (S.bridge_dir / ".env").write_text(f"SLACK_BOT_TOKEN={TOKEN}\n")
    calls = []

    def fetch(token, uid):
        calls.append(uid)
        assert token == TOKEN
        return {"ok": True, "user": PEOPLE[uid]} if uid in PEOPLE else {"ok": False, "error": "user_not_found"}
    monkeypatch.setattr(slack_users, "_fetch", fetch)
    return calls


def _asked_by(env, *users):
    t0 = time.time() - 3600
    write_transcript(env, SID, [{"q": "map empty?", "at": t0, "answer": "ok"}])
    S.threads_file.write_text(json.dumps({"C1:100.1": SID}))
    write_turns([e for i, u in enumerate(users) for e in (
        {"event": "start", "ts": t0 + i, "key": "C1:100.1", "channel": "C1", "thread_ts": "100.1", "user": u},
        {"event": "end", "ts": t0 + i + 1, "key": "C1:100.1", "session_id": SID, "outcome": "ok"})])


def test_names_are_looked_up_once_and_kept(slack):
    assert slack_users.names([GAL, YON, "not an id", ""]) == ({GAL: "Gal Badichi", YON: "Yonatan Shapira"}, None)
    assert slack_users.names([GAL, YON]) == ({GAL: "Gal Badichi", YON: "Yonatan Shapira"}, None)
    assert slack == [GAL, YON]   # the second time from the cache
    assert TOKEN not in (S.state_dir / "slack_users.json").read_text()


def test_a_name_is_asked_again_after_a_week_and_kept_meanwhile(slack, monkeypatch):
    slack_users.names([GAL])
    later = time.time() + slack_users.FRESH_S + 1
    monkeypatch.setattr(slack_users.time, "time", lambda: later)
    PEOPLE[GAL]["profile"]["real_name"] = "Gal B."
    try:
        assert slack_users.names([GAL])[0] == {GAL: "Gal B."} and slack == [GAL, GAL]
    finally:
        PEOPLE[GAL]["profile"]["real_name"] = "Gal Badichi"


def test_an_unknown_id_is_tried_again_only_later(slack, monkeypatch):
    unknown = "U0ZZZZZZZZ9"
    assert slack_users.names([unknown]) == ({}, None)
    assert slack_users.names([unknown]) == ({}, None) and slack == [unknown]
    later = time.time() + slack_users.RETRY_S + 1
    monkeypatch.setattr(slack_users.time, "time", lambda: later)
    slack_users.names([unknown])
    assert slack == [unknown, unknown]


@pytest.mark.parametrize("error", ["missing_scope", "invalid_auth"])
def test_an_app_problem_is_reported_and_nothing_is_cached(slack, monkeypatch, error):
    monkeypatch.setattr(slack_users, "_fetch", lambda token, uid: {"ok": False, "error": error})
    names, problem = slack_users.names([GAL, YON])
    assert names == {} and problem == slack_users.APP_ERRORS[error]
    assert not (S.state_dir / "slack_users.json").exists()


def test_without_a_token_or_network_the_ids_stay(env, monkeypatch):
    assert slack_users.names([GAL]) == ({}, slack_users.APP_ERRORS["not_authed"])
    (S.bridge_dir / ".env").write_text(f"SLACK_BOT_TOKEN={TOKEN}\n")
    assert slack_users.names([GAL]) == ({}, "Slack could not be reached to look up names")


def test_lookups_per_call_are_capped(slack):
    many = [f"U0AAAAA{i:04}" for i in range(slack_users.MAX_LOOKUPS + 5)]
    slack_users.names(many)
    assert len(slack) == slack_users.MAX_LOOKUPS


def test_a_name_with_a_control_character_is_not_used(slack):
    PEOPLE["U0CCCCCCCC1"] = {"real_name": "Evil\nName", "name": "plain"}
    try:
        assert slack_users.names(["U0CCCCCCCC1"])[0] == {"U0CCCCCCCC1": "plain"}
    finally:
        del PEOPLE["U0CCCCCCCC1"]


def test_conversations_show_and_find_who_asked_by_name(client, env, slack):
    _asked_by(env, GAL, YON)
    r = client.get("/api/conversations").json()
    assert r["names"] == {GAL: "Gal Badichi", YON: "Yonatan Shapira"} and r["names_problem"] is None
    assert [t["key"] for t in client.get("/api/conversations?q=shapira").json()["threads"]] == ["C1:100.1"]
    assert client.get("/api/conversations?q=nobody").json()["threads"] == []
    d = client.get("/api/conversations/C1:100.1").json()
    assert d["names"][GAL] == "Gal Badichi" and TOKEN not in json.dumps(d) + json.dumps(r)
